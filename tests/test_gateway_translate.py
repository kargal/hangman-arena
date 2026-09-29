from pathlib import Path
from typing import Any

import pytest

from arena_gateway.config import GatewayConfig, ModelRoute, Provider, load_config
from arena_gateway.signature import decode_reasoning, encode_reasoning
from arena_gateway.translate import (
    build_upstream_request,
    convert_response,
    convert_usage,
    log_usage,
    upstream_error_message,
)

ROOT = Path(__file__).resolve().parents[1]
PROVIDER = Provider(name="openai", base_url="https://upstream.test/v1", api_key="sk-test", max_retries=2)


def make_config(**overrides: Any) -> GatewayConfig:
    models = {
        "gpt-5.6-sol": ModelRoute(
            "gpt-5.6-sol", PROVIDER, "gpt-5.6-sol", reasoning_effort="medium", max_output_tokens=128000
        ),
        "plain-model": ModelRoute("plain-model", PROVIDER, "plain-upstream", reasoning=False),
    }
    return GatewayConfig(
        default_model="gpt-5.6-sol",
        models=models,
        aliases={"claude-*haiku*": "gpt-5.6-sol"},
        **overrides,
    )


def req(**body: Any) -> dict[str, Any]:
    return {"model": "gpt-5.6-sol", "max_tokens": 32000, "messages": [{"role": "user", "content": "hi"}], **body}


def test_shipped_config_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    config = load_config(ROOT / "configs" / "gateway" / "models.yaml")
    route = config.resolve("gpt-5.6-sol")
    assert route.reasoning_effort == "medium"
    assert route.provider.api_key == "sk-from-env"
    assert route.provider.base_url == "https://api.openai.com/v1"
    assert config.max_output_tokens_floor == 64000


def test_openai_base_url_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1/")
    config = load_config(ROOT / "configs" / "gateway" / "models.yaml")
    assert config.resolve("gpt-6-sol").provider.base_url == "https://proxy.example/v1"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("gpt-5.6-sol", "gpt-5.6-sol"),
        ("plain-model", "plain-model"),
        ("gpt-5.6-sol[1m]", "gpt-5.6-sol"),
        ("claude-haiku-4-5-20251001", "gpt-5.6-sol"),
        ("something-unknown", "gpt-5.6-sol"),
        (None, "gpt-5.6-sol"),
    ],
)
def test_model_resolution(requested: str | None, expected: str) -> None:
    assert make_config().resolve(requested).name == expected


def test_basic_request_shape() -> None:
    payload, ctx = build_upstream_request(
        req(stream=True, temperature=0.2, top_p=0.9, thinking={"type": "adaptive"}), make_config()
    )
    assert payload["model"] == "gpt-5.6-sol"
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert "temperature" not in payload and "top_p" not in payload
    assert payload["input"] == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    assert ctx.stream


def test_sampling_params_pass_for_non_reasoning_models() -> None:
    payload, _ = build_upstream_request(req(model="plain-model", temperature=0.2, top_p=0.9, top_k=5), make_config())
    assert payload["temperature"] == 0.2 and payload["top_p"] == 0.9
    assert "top_k" not in payload and "reasoning" not in payload


@pytest.mark.parametrize(("client", "expected"), [(32000, 64000), (8000, 64000), (100000, 100000), (200000, 200000)])
def test_max_output_tokens_floor_never_below_client(client: int, expected: int) -> None:
    payload, _ = build_upstream_request(req(max_tokens=client), make_config())
    assert payload["max_output_tokens"] == expected


def test_floor_is_capped_by_model_limit() -> None:
    payload, _ = build_upstream_request(req(max_tokens=1000), make_config(max_output_tokens_floor=500000))
    assert payload["max_output_tokens"] == 128000


def test_system_prompt_string_and_blocks() -> None:
    payload, _ = build_upstream_request(req(system="be brief"), make_config())
    assert payload["instructions"] == "be brief"
    blocks = [
        {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.306;"},
        {"type": "text", "text": "You are an agent.", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "Use tools."},
    ]
    payload, _ = build_upstream_request(req(system=blocks), make_config())
    assert payload["instructions"] == "You are an agent.\n\nUse tools."


def test_mid_conversation_system_message_becomes_developer() -> None:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "system", "content": [{"type": "text", "text": "# Environment"}]},
    ]
    payload, _ = build_upstream_request(req(messages=messages), make_config())
    assert payload["input"][1] == {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": "# Environment"}],
    }


def test_tool_use_and_tool_results() -> None:
    messages = [
        {"role": "user", "content": "list files"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Listing."},
                {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}},
                {"type": "tool_use", "id": "call_2", "name": "Read", "input": {"file_path": "/x"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "a.txt\nb.txt"},
                {
                    "type": "tool_result",
                    "tool_use_id": "call_2",
                    "is_error": True,
                    "content": [{"type": "text", "text": "<tool_use_error>File does not exist.</tool_use_error>"}],
                },
                {"type": "text", "text": "continue"},
            ],
        },
    ]
    payload, _ = build_upstream_request(req(messages=messages), make_config())
    items = payload["input"]
    assert items[1] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Listing."}],
    }
    assert items[2] == {"type": "function_call", "call_id": "call_1", "name": "Bash", "arguments": '{"command": "ls"}'}
    assert items[3]["call_id"] == "call_2"
    assert items[4] == {"type": "function_call_output", "call_id": "call_1", "output": "a.txt\nb.txt"}
    assert items[5]["output"] == "Error: <tool_use_error>File does not exist.</tool_use_error>"
    assert items[6] == {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]}


def test_images_in_user_content_and_tool_results() -> None:
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "look"}, image]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_9", "name": "Read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_9", "content": [image]}]},
    ]
    payload, _ = build_upstream_request(req(messages=messages), make_config())
    assert payload["input"][0]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
    assert payload["input"][2]["type"] == "function_call_output"
    assert payload["input"][3] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
    }


def test_tool_definitions() -> None:
    tools = [
        {
            "name": "Bash",
            "description": "Run a command",
            "input_schema": {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        {
            "name": "DeferredToolPlaceholder",
            "description": "never call",
            "input_schema": {"type": "object", "properties": {}},
            "defer_loading": True,
        },
        {"type": "code_execution_20250825", "name": "code_execution"},
    ]
    payload, ctx = build_upstream_request(
        req(tools=tools, tool_choice={"type": "any", "disable_parallel_tool_use": True}), make_config()
    )
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "Bash",
            "description": "Run a command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
            "strict": False,
        }
    ]
    assert payload["tool_choice"] == "required"
    assert payload["parallel_tool_calls"] is False
    assert "tool.code_execution_20250825" in ctx.dropped


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ({"type": "auto"}, "auto"),
        ({"type": "none"}, "none"),
        ({"type": "tool", "name": "Bash"}, {"type": "function", "name": "Bash"}),
    ],
)
def test_tool_choice(choice: dict[str, Any], expected: Any) -> None:
    tools = [{"name": "Bash", "input_schema": {"type": "object", "properties": {}}}]
    payload, _ = build_upstream_request(req(tools=tools, tool_choice=choice), make_config())
    assert payload["tool_choice"] == expected


def test_reasoning_round_trip_from_thinking_blocks() -> None:
    signature = encode_reasoning({"id": "rs_1", "encrypted_content": "ENC=="})
    assert decode_reasoning(signature) == {"id": "rs_1", "enc": "ENC=="}
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Plan it.", "signature": signature},
                {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}]},
    ]
    payload, _ = build_upstream_request(req(messages=messages), make_config())
    assert payload["input"][1] == {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "Plan it."}],
        "encrypted_content": "ENC==",
    }
    assert payload["input"][2]["type"] == "function_call"


def test_foreign_thinking_signatures_are_dropped() -> None:
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "x", "signature": "EqQBCkYIBx..."},
                {"type": "text", "text": "done"},
            ],
        },
    ]
    payload, ctx = build_upstream_request(req(messages=messages), make_config())
    assert [i["type"] for i in payload["input"]] == ["message", "message"]
    assert any("foreign signature" in d for d in ctx.dropped)


def test_prompt_cache_key_from_session() -> None:
    body = req(metadata={"user_id": '{"device_id":"d","session_id":"abc-123"}'})
    payload, _ = build_upstream_request(body, make_config())
    key = payload["prompt_cache_key"]
    assert key.startswith("arena-") and len(key) == 38
    payload2, _ = build_upstream_request(req(), make_config(), {"X-Claude-Code-Session-Id": "abc-123"})
    assert payload2["prompt_cache_key"] == key


def test_convert_non_streaming_response() -> None:
    _, ctx = build_upstream_request(req(), make_config())
    response = {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {
                "id": "rs_1",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "think"}],
                "encrypted_content": "E",
            },
            {"id": "m1", "type": "message", "content": [{"type": "output_text", "text": "Running it."}]},
            {
                "id": "fc",
                "type": "function_call",
                "call_id": "call_7",
                "name": "Bash",
                "arguments": '{"command":"pwd"}',
            },
        ],
        "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 40}, "output_tokens": 30},
    }
    message = convert_response(response, ctx)
    assert [b["type"] for b in message["content"]] == ["thinking", "text", "tool_use"]
    assert decode_reasoning(message["content"][0]["signature"]) == {"id": "rs_1", "enc": "E"}
    assert message["content"][2] == {"type": "tool_use", "id": "call_7", "name": "Bash", "input": {"command": "pwd"}}
    assert message["stop_reason"] == "tool_use"
    assert message["usage"] == {
        "input_tokens": 60,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 40,
        "output_tokens": 30,
    }
    assert message["model"] == "gpt-5.6-sol"


def test_stop_reasons_and_stop_sequences() -> None:
    _, ctx = build_upstream_request(req(stop_sequences=["</answer>"]), make_config())
    text_item = {"type": "message", "content": [{"type": "output_text", "text": "42</answer> trailing"}]}
    message = convert_response({"status": "completed", "output": [text_item]}, ctx)
    assert message["content"] == [{"type": "text", "text": "42"}]
    assert (message["stop_reason"], message["stop_sequence"]) == ("stop_sequence", "</answer>")

    _, ctx = build_upstream_request(req(), make_config())
    incomplete = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}
    assert convert_response(incomplete, ctx)["stop_reason"] == "max_tokens"
    assert convert_response({"status": "completed", "output": [text_item]}, ctx)["stop_reason"] == "end_turn"


def test_context_length_errors_read_as_prompt_too_long() -> None:
    body = {
        "error": {"message": "Your input exceeds the context window of this model.", "code": "context_length_exceeded"}
    }
    assert upstream_error_message(400, body).startswith("prompt is too long")
    assert upstream_error_message(500, {"error": {"message": "boom"}}) == "upstream error 500: boom"


def test_usage_with_cache_writes() -> None:
    # GPT-5.6 usage: input_tokens = uncached + cached (reads) + cache_write_tokens; the three do not overlap.
    usage = {
        "input_tokens": 13211,
        "input_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 12000},
        "output_tokens": 86,
        "output_tokens_details": {"reasoning_tokens": 45},
    }
    assert convert_usage(usage) == {
        "input_tokens": 211,
        "cache_creation_input_tokens": 12000,
        "cache_read_input_tokens": 1000,
        "output_tokens": 86,
    }
    assert log_usage(usage) == {
        "input_tokens": 13211,
        "cached_input_tokens": 1000,
        "cache_write_tokens": 12000,
        "output_tokens": 86,
    }
    assert log_usage(None) == {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0}
