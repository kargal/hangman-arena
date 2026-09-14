import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from arena_gateway.app import create_app
from arena_gateway.upstream import Upstream
from tests.gateway_fixtures import (
    ENC,
    assemble_message,
    parse_anthropic_sse,
    responses_stream_text,
    responses_stream_tool_call,
    to_sse,
)
from tests.test_gateway_translate import make_config


class FakeUpstream:
    """Scripted OpenAI Responses endpoint: each call pops the next (status, body) pair."""

    def __init__(self, script: list[tuple[int, Any]]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        assert request.headers["authorization"] == "Bearer sk-test"
        payload = json.loads(request.content)
        self.requests.append(payload)
        status, body = self.script.pop(0)
        if isinstance(body, list):  # SSE events
            return httpx.Response(status, content=to_sse(body), headers={"content-type": "text/event-stream"})
        return httpx.Response(status, json=body, headers={"retry-after": "0"} if status == 429 else {})


async def _no_sleep(_: float) -> None:
    return None


def client_for(fake: FakeUpstream, tmp_path: Path, **config: Any) -> TestClient:
    upstream = Upstream(transport=httpx.MockTransport(fake.handler), sleep=_no_sleep)
    app = create_app(make_config(log_file=tmp_path / "gateway.jsonl", **config), upstream=upstream)
    return TestClient(app)


def body(**extra: Any) -> dict[str, Any]:
    return {"model": "gpt-5.6-sol", "max_tokens": 32000, "messages": [{"role": "user", "content": "hi"}], **extra}


def log_records(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (tmp_path / "gateway.jsonl").read_text().splitlines()]


def test_startup_endpoints_do_not_404(tmp_path: Path) -> None:
    with client_for(FakeUpstream([]), tmp_path) as c:
        assert c.head("/api/hello").status_code == 200
        assert c.head("/").status_code == 200
        assert c.get("/healthz").json()["default_model"] == "gpt-5.6-sol"
        assert [m["id"] for m in c.get("/v1/models").json()["data"]] == ["gpt-5.6-sol", "plain-model"]
        assert c.get("/v1/models/claude-haiku-4-5").json()["id"] == "gpt-5.6-sol"
        tokens = c.post("/v1/messages/count_tokens?beta=true", json=body()).json()["input_tokens"]
        assert tokens > 0
        r = c.post("/v1/unknown")
        assert r.status_code == 404 and r.json()["type"] == "error"


def test_streaming_tool_call_end_to_end(tmp_path: Path) -> None:
    fake = FakeUpstream([(200, responses_stream_tool_call())])
    with client_for(fake, tmp_path) as c:
        r = c.post("/v1/messages?beta=true", json=body(stream=True), headers={"x-claude-code-session-id": "s1"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = parse_anthropic_sse(r.text)
    message = assemble_message(events)
    assert [b["type"] for b in message["content"]] == ["thinking", "tool_use"]
    assert message["stop_reason"] == "tool_use"
    sent = fake.requests[0]
    assert sent["stream"] is True and sent["store"] is False
    assert sent["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert sent["max_output_tokens"] == 64000
    record = log_records(tmp_path)[0]
    assert record["stop_reason"] == "tool_use"
    assert record["usage"]["output_tokens"] == 80 and record["reasoning_tokens"] == 50
    assert record["completed"] is True and record["session"] == "s1"


def test_reasoning_persists_across_turns(tmp_path: Path) -> None:
    fake = FakeUpstream([(200, responses_stream_tool_call()), (200, responses_stream_text(["Done."]))])
    with client_for(fake, tmp_path) as c:
        first = assemble_message(parse_anthropic_sse(c.post("/v1/messages", json=body(stream=True)).text))
        followup = body(
            stream=True,
            messages=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": first["content"]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_XYZ", "content": ""}]},
            ],
        )
        second = assemble_message(parse_anthropic_sse(c.post("/v1/messages", json=followup).text))
    assert second["content"] == [{"type": "text", "text": "Done."}]
    replay = fake.requests[1]["input"]
    assert replay[1] == {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "**Plan**\n\nCreate the file."}],
        "encrypted_content": ENC,
    }
    assert replay[2] == {
        "type": "function_call",
        "call_id": "call_XYZ",
        "name": "Bash",
        "arguments": '{"command": "echo hi > a.txt"}',
    }
    assert replay[3] == {"type": "function_call_output", "call_id": "call_XYZ", "output": ""}


def test_non_streaming(tmp_path: Path) -> None:
    upstream_body = {
        "id": "resp_9",
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "pong"}]}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }
    fake = FakeUpstream([(200, upstream_body)])
    with client_for(fake, tmp_path) as c:
        r = c.post("/v1/messages", json=body())
    assert r.status_code == 200
    assert r.json()["content"] == [{"type": "text", "text": "pong"}]
    assert r.json()["stop_reason"] == "end_turn"
    assert fake.requests[0]["stream"] is False


def test_retries_on_429_and_5xx(tmp_path: Path) -> None:
    fake = FakeUpstream(
        [
            (429, {"error": {"message": "rate limited"}}),
            (503, {"error": {"message": "overloaded"}}),
            (200, responses_stream_text(["ok"])),
        ]
    )
    with client_for(fake, tmp_path) as c:
        r = c.post("/v1/messages", json=body(stream=True))
    assert assemble_message(parse_anthropic_sse(r.text))["content"][0]["text"] == "ok"
    assert len(fake.requests) == 3
    assert log_records(tmp_path)[0]["retries"] == 2


def test_retries_exhausted_returns_anthropic_error(tmp_path: Path) -> None:
    fake = FakeUpstream([(500, {"error": {"message": "boom"}})] * 3)
    with client_for(fake, tmp_path) as c:
        r = c.post("/v1/messages", json=body(stream=True))
    assert r.status_code == 500
    assert r.json()["error"]["type"] == "api_error"
    assert len(fake.requests) == 3  # 1 + max_retries(2)


def test_client_errors_are_not_retried(tmp_path: Path) -> None:
    fake = FakeUpstream(
        [(400, {"error": {"message": "exceeds the context window", "code": "context_length_exceeded"}})]
    )
    with client_for(fake, tmp_path) as c:
        r = c.post("/v1/messages", json=body())
    assert r.status_code == 400
    assert r.json()["error"]["message"].startswith("prompt is too long")
    assert len(fake.requests) == 1


def test_gateway_api_key(tmp_path: Path) -> None:
    fake = FakeUpstream([(200, responses_stream_text(["ok"]))])
    with client_for(fake, tmp_path, api_key="gw-secret") as c:
        assert c.post("/v1/messages", json=body()).status_code == 401
        r = c.post("/v1/messages", json=body(stream=True), headers={"Authorization": "Bearer gw-secret"})
        assert r.status_code == 200
        assert c.post("/v1/messages/count_tokens", json=body(), headers={"x-api-key": "gw-secret"}).status_code == 200


def test_bad_request_body(tmp_path: Path) -> None:
    with client_for(FakeUpstream([]), tmp_path) as c:
        r = c.post("/v1/messages", json={"model": "x"})
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize("log_bodies", [False, True])
def test_log_bodies_flag(tmp_path: Path, log_bodies: bool) -> None:
    fake = FakeUpstream([(200, responses_stream_text(["ok"]))])
    with client_for(fake, tmp_path, log_bodies=log_bodies) as c:
        c.post("/v1/messages", json=body(stream=True))
    assert ("upstream_payload" in log_records(tmp_path)[0]) is log_bodies


def test_cache_write_tokens_are_logged_and_reported(tmp_path: Path) -> None:
    usage = {
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 30, "cache_write_tokens": 60},
        "output_tokens": 2,
    }
    text_item = {"type": "message", "content": [{"type": "output_text", "text": "pong"}]}
    stream = responses_stream_text(["pong"])
    stream[-1]["response"]["usage"] = usage
    fake = FakeUpstream(
        [(200, {"id": "resp_1", "status": "completed", "output": [text_item], "usage": usage}), (200, stream)]
    )
    with client_for(fake, tmp_path) as c:
        plain = c.post("/v1/messages", json=body()).json()
        streamed = assemble_message(parse_anthropic_sse(c.post("/v1/messages", json=body(stream=True)).text))
    for message in (plain, streamed):
        assert message["usage"]["input_tokens"] == 10
        assert message["usage"]["cache_read_input_tokens"] == 30
        assert message["usage"]["cache_creation_input_tokens"] == 60
    expected = {"input_tokens": 100, "cached_input_tokens": 30, "cache_write_tokens": 60, "output_tokens": 2}
    assert [r["usage"] for r in log_records(tmp_path)] == [expected, expected]
