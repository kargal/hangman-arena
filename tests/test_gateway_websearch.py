"""Web search: Anthropic ``web_search_*`` server tool <-> Responses hosted ``web_search`` tool (mocked upstream)."""

import json
from pathlib import Path
from typing import Any

import pytest

from arena_gateway.translate import build_upstream_request, convert_messages, convert_response
from tests.gateway_fixtures import parse_anthropic_sse
from tests.test_gateway_app import FakeUpstream, client_for, log_records
from tests.test_gateway_translate import make_config

STATICE = "https://www.statice.is/publications/news-archive/inhabitants/population-1-january-2026/"
WIKI = "https://en.wikipedia.org/wiki/Demographics_of_Iceland"
ANSWER = (
    f"Statistics Iceland put the population at 394,324 on 1 January 2026. ([statice.is]({STATICE}?utm_source=openai))"
)


def claude_code_search_request(**extra: Any) -> dict[str, Any]:
    """The side request Claude Code's WebSearch tool sends (2.1.x)."""
    return {
        "model": "gpt-5.6-sol",
        "max_tokens": 32000,
        "system": [{"type": "text", "text": "You run a web search for another Claude model ..."}],
        "messages": [{"role": "user", "content": "Perform a web search for the query: Iceland population 2026"}],
        "tools": [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "allowed_domains": ["https://statice.is/", "en.wikipedia.org"],
                "max_uses": 8,
            }
        ],
        "tool_choice": {"type": "tool", "name": "web_search"},
        "thinking": {"type": "disabled"},
        "stream": True,
        **extra,
    }


def search_call(*, status: str = "completed", sources: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": "ws_075af7b8db3bf535016ad8b1aad79887d2864dc1338c6587aa",
        "type": "web_search_call",
        "status": status,
        "action": {
            "type": "search",
            "query": "Iceland population estimate 2026",
            "queries": ["Iceland population estimate 2026"],
            "sources": [
                {"type": "url", "url": u} for u in (sources if sources is not None else [STATICE, WIKI, STATICE])
            ],
        },
    }


def answer_message(text: str = ANSWER) -> dict[str, Any]:
    start = text.find("([statice")
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [
                    {
                        "type": "url_citation",
                        "start_index": start,
                        "end_index": len(text),
                        "title": "Population 1 January 2026 - Statistics Iceland",
                        "url": STATICE + "?utm_source=openai",
                    }
                ]
                if "([statice" in text
                else [],
            }
        ],
    }


def upstream_response(*output: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "resp_ws",
        "object": "response",
        "status": "completed",
        "output": [{"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "enc"}, *output],
        "usage": {
            "input_tokens": 12535,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 4367},
            "output_tokens": 122,
            "output_tokens_details": {"reasoning_tokens": 41},
        },
    }


def claude_code_results(content: list[dict[str, Any]]) -> list[Any]:
    """What Claude Code's WebSearch tool extracts from the reply (mirrors its parser)."""
    results: list[Any] = []
    text = ""
    for block in content:
        if block["type"] == "server_tool_use":
            if text.strip():
                results.append(text.strip())
            text = ""
        elif block["type"] == "web_search_tool_result":
            if not isinstance(block["content"], list):
                results.append(f"Web search error: {block['content']['error_code']}")
                continue
            results.append([{"title": r["title"], "url": r["url"]} for r in block["content"]])
        elif block["type"] == "text":
            text += block["text"]
    if text.strip():
        results.append(text.strip())
    return results


# --------------------------------------------------------------------------- request


def test_request_uses_hosted_web_search() -> None:
    payload, ctx = build_upstream_request(claude_code_search_request(), make_config())
    assert payload["tools"] == [
        {"type": "web_search", "filters": {"allowed_domains": ["statice.is", "en.wikipedia.org"]}}
    ]
    assert payload["tool_choice"] == {"type": "web_search"}
    assert payload["max_tool_calls"] == 8
    assert "web_search_call.action.sources" in payload["include"]
    assert "reasoning.encrypted_content" in payload["include"]
    assert payload["stream"] is False  # served from one complete upstream response
    assert ctx.stream is True and ctx.web_search_forced and ctx.web_search_tools == ["web_search"]
    assert not ctx.dropped


def test_blocked_domains_and_user_location() -> None:
    tool = {
        "type": "web_search_20250305",
        "name": "web_search",
        "blocked_domains": ["http://example.com"],
        "user_location": {
            "type": "approximate",
            "city": "Reykjavik",
            "country": "IS",
            "timezone": "Atlantic/Reykjavik",
        },
    }
    bash = {"name": "Bash", "description": "Run a command", "input_schema": {"type": "object"}}
    payload, ctx = build_upstream_request(
        claude_code_search_request(tools=[tool, bash], tool_choice={"type": "auto"}), make_config()
    )
    assert payload["tools"][0] == {
        "type": "web_search",
        "filters": {"blocked_domains": ["example.com"]},
        "user_location": {
            "type": "approximate",
            "city": "Reykjavik",
            "country": "IS",
            "timezone": "Atlantic/Reykjavik",
        },
    }
    assert payload["tool_choice"] == "auto"
    assert "max_tool_calls" not in payload
    assert not ctx.web_search_forced


@pytest.mark.parametrize("choice", [{"type": "auto"}, None])
def test_web_search_as_the_only_tool_is_required(choice: dict[str, Any] | None) -> None:
    """Claude Code 2.1.306 sends its WebSearch side request with tool_choice auto: still a search request."""
    payload, ctx = build_upstream_request(claude_code_search_request(tool_choice=choice), make_config())
    assert payload["tool_choice"] == {"type": "web_search"}
    assert ctx.web_search_forced


def test_plain_requests_are_unchanged() -> None:
    payload, ctx = build_upstream_request(claude_code_search_request(tools=[], tool_choice=None), make_config())
    assert payload["stream"] is True
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert not ctx.wants_web_search


def test_earlier_search_blocks_become_assistant_text() -> None:
    _, ctx = build_upstream_request(claude_code_search_request(), make_config())
    items = convert_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "srvtoolu_1",
                        "content": [
                            {
                                "type": "web_search_result",
                                "url": WIKI,
                                "title": "Demographics",
                                "encrypted_content": "x",
                            }
                        ],
                    },
                    {
                        "type": "text",
                        "text": "Summary.",
                        "citations": [{"type": "web_search_result_location", "url": WIKI}],
                    },
                ],
            }
        ],
        ctx,
    )
    assert items == [
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": f"[web search: q]\n[web search results]\n- Demographics <{WIKI}>\nSummary.",
                }
            ],
        }
    ]
    assert not ctx.dropped


# --------------------------------------------------------------------------- response


def test_search_results_titles_and_citations() -> None:
    _, ctx = build_upstream_request(claude_code_search_request(stream=False), make_config())
    reply = convert_response(upstream_response(search_call(), answer_message()), ctx)
    types = [b["type"] for b in reply["content"]]
    assert types == ["thinking", "server_tool_use", "web_search_tool_result", "text", "text"]
    use, result = reply["content"][1], reply["content"][2]
    assert use["name"] == "web_search" and use["input"] == {"query": "Iceland population estimate 2026"}
    assert use["id"].startswith("srvtoolu_") and result["tool_use_id"] == use["id"]
    assert [(r["url"], r["title"]) for r in result["content"]] == [
        (STATICE, "Population 1 January 2026 - Statistics Iceland"),  # title from the citation, duplicate removed
        (WIKI, WIKI),  # not cited -> no upstream title -> URL
    ]
    assert all(r["type"] == "web_search_result" and r["page_age"] is None for r in result["content"])
    plain, cited = reply["content"][3], reply["content"][4]
    assert "citations" not in plain and plain["text"].startswith("Statistics Iceland")
    assert cited["citations"][0]["type"] == "web_search_result_location"
    assert cited["citations"][0]["url"] == STATICE  # utm_source=openai removed
    assert plain["text"] + cited["text"] == ANSWER
    assert reply["stop_reason"] == "end_turn"
    assert reply["usage"]["server_tool_use"] == {"web_search_requests": 1}
    assert ctx.web_search == {
        "calls": 1,
        "searches": 1,
        "failed": 0,
        "sources": 2,
        "queries": ["Iceland population estimate 2026"],
        "other_actions": [],
        "text_suppressed": False,
    }
    assert claude_code_results(reply["content"])[0] == [
        {"title": "Population 1 January 2026 - Statistics Iceland", "url": STATICE},
        {"title": WIKI, "url": WIKI},
    ]


def test_failed_search_returns_error_not_model_text() -> None:
    _, ctx = build_upstream_request(claude_code_search_request(stream=False), make_config())
    reply = convert_response(
        upstream_response(search_call(status="failed"), answer_message("From memory: 390,000.")), ctx
    )
    assert [b["type"] for b in reply["content"]] == ["thinking", "server_tool_use", "web_search_tool_result"]
    assert reply["content"][2]["content"] == {"type": "web_search_tool_result_error", "error_code": "unavailable"}
    assert ctx.web_search is not None and ctx.web_search["text_suppressed"] and ctx.web_search["failed"] == 1
    assert claude_code_results(reply["content"]) == ["Web search error: unavailable"]


@pytest.mark.parametrize("choice", [{"type": "tool", "name": "web_search"}, {"type": "auto"}])
def test_required_search_that_never_ran_returns_error(choice: dict[str, Any]) -> None:
    _, ctx = build_upstream_request(claude_code_search_request(stream=False, tool_choice=choice), make_config())
    reply = convert_response(upstream_response(answer_message("Iceland has about 390,000 people.")), ctx)
    assert [b["type"] for b in reply["content"]] == ["thinking", "server_tool_use", "web_search_tool_result"]
    assert claude_code_results(reply["content"]) == ["Web search error: unavailable"]
    assert ctx.web_search is not None and ctx.web_search["searches"] == 0 and ctx.web_search["text_suppressed"]


def test_unforced_answer_without_search_is_kept() -> None:
    bash = {"name": "Bash", "description": "Run a command", "input_schema": {"type": "object"}}
    request = claude_code_search_request(stream=False, tool_choice={"type": "auto"})
    request["tools"].append(bash)
    _, ctx = build_upstream_request(request, make_config())
    reply = convert_response(upstream_response(answer_message("No search needed.")), ctx)
    assert [b["type"] for b in reply["content"]] == ["thinking", "text"]
    assert reply["usage"]["server_tool_use"] == {"web_search_requests": 0}


def test_open_page_actions_are_counted_but_not_results() -> None:
    _, ctx = build_upstream_request(claude_code_search_request(stream=False), make_config())
    open_page = {
        "id": "ws_2",
        "type": "web_search_call",
        "status": "completed",
        "action": {"type": "open_page", "url": WIKI},
    }
    reply = convert_response(upstream_response(search_call(sources=[]), open_page, answer_message()), ctx)
    assert [b["type"] for b in reply["content"]].count("server_tool_use") == 1
    assert reply["content"][2]["content"] == []
    assert (
        ctx.web_search is not None and ctx.web_search["calls"] == 2 and ctx.web_search["other_actions"] == ["open_page"]
    )


# --------------------------------------------------------------------------- app


def assemble(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    message: dict[str, Any] = {}
    partial: dict[int, str] = {}
    for name, data in events:
        if name == "message_start":
            message = {**data["message"], "content": []}
        elif name == "content_block_start":
            message["content"].append(json.loads(json.dumps(data["content_block"])))
        elif name == "content_block_delta":
            block, delta = message["content"][data["index"]], data["delta"]
            if delta["type"] == "text_delta":
                block["text"] += delta["text"]
            elif delta["type"] == "citations_delta":
                block.setdefault("citations", []).append(delta["citation"])
            elif delta["type"] == "thinking_delta":
                block["thinking"] += delta["thinking"]
            elif delta["type"] == "signature_delta":
                block["signature"] = delta["signature"]
            elif delta["type"] == "input_json_delta":
                partial[data["index"]] = partial.get(data["index"], "") + delta["partial_json"]
        elif name == "content_block_stop" and data["index"] in partial:
            message["content"][data["index"]]["input"] = json.loads(partial[data["index"]])
        elif name == "message_delta":
            message.update(data["delta"])
            message["usage"] = data["usage"]
    return message


def test_streaming_web_search_end_to_end(tmp_path: Path) -> None:
    fake = FakeUpstream([(200, upstream_response(search_call(), answer_message()))])
    with client_for(fake, tmp_path) as c:
        r = c.post("/v1/messages?beta=true", json=claude_code_search_request())
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = parse_anthropic_sse(r.text)
    assert fake.requests[0]["stream"] is False and fake.requests[0]["tool_choice"] == {"type": "web_search"}
    names = [n for n, _ in events]
    assert names[:2] == ["message_start", "ping"] and names[-2:] == ["message_delta", "message_stop"]
    assert ("content_block_delta", "citations_delta") in [(n, d.get("delta", {}).get("type")) for n, d in events]
    message = assemble(events)
    assert [b["type"] for b in message["content"]] == [
        "thinking",
        "server_tool_use",
        "web_search_tool_result",
        "text",
        "text",
    ]
    assert message["content"][1]["input"] == {"query": "Iceland population estimate 2026"}
    assert message["content"][2]["content"][0]["url"] == STATICE
    assert message["content"][4]["citations"][0]["url"] == STATICE
    assert message["usage"]["server_tool_use"] == {"web_search_requests": 1}
    assert message["stop_reason"] == "end_turn"
    record = log_records(tmp_path)[0]
    assert record["status"] == 200 and record["stream"] is True
    assert record["web_search_calls"] == 1 and record["web_search"]["sources"] == 2
    assert record["content_blocks"] == ["thinking", "server_tool_use", "web_search_tool_result", "text", "text"]


def test_non_streaming_web_search(tmp_path: Path) -> None:
    fake = FakeUpstream([(200, upstream_response(search_call(), answer_message()))])
    with client_for(fake, tmp_path) as c:
        r = c.post("/v1/messages", json=claude_code_search_request(stream=False))
    assert r.status_code == 200
    reply = r.json()
    assert [b["type"] for b in reply["content"]][1:3] == ["server_tool_use", "web_search_tool_result"]
    assert log_records(tmp_path)[0]["web_search_calls"] == 1


def test_upstream_failure_is_an_error_not_text(tmp_path: Path) -> None:
    fake = FakeUpstream([(400, {"error": {"message": "web_search is not supported", "code": "unsupported"}})])
    with client_for(fake, tmp_path) as c:
        r = c.post("/v1/messages", json=claude_code_search_request())
    assert r.status_code == 400 and r.json()["type"] == "error"
    assert "content" not in r.json()
    assert log_records(tmp_path)[0]["error"].startswith("upstream error 400")
