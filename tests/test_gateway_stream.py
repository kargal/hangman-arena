import json

from arena_gateway.signature import decode_reasoning
from arena_gateway.streaming import StopSequenceFilter, StreamTranslator
from arena_gateway.translate import build_upstream_request
from tests.gateway_fixtures import ENC, assemble_message, responses_stream_text, responses_stream_tool_call
from tests.test_gateway_translate import make_config, req


def run(events, **body):  # type: ignore[no-untyped-def]
    _, ctx = build_upstream_request(req(stream=True, **body), make_config())
    translator = StreamTranslator(ctx)
    out = list(translator.start())
    for event in events:
        out += translator.feed(event)
    out += translator.end_of_stream()
    return out, translator


def test_exact_event_sequence_for_reasoning_and_tool_call() -> None:
    out, translator = run(responses_stream_tool_call())
    names = [(n, d.get("delta", {}).get("type") or d.get("content_block", {}).get("type")) for n, d in out]
    assert names == [
        ("message_start", None),
        ("ping", None),
        ("content_block_start", "thinking"),
        ("content_block_delta", "thinking_delta"),
        ("content_block_delta", "thinking_delta"),  # "\n\n" between summary parts
        ("content_block_delta", "thinking_delta"),
        ("content_block_delta", "thinking_delta"),
        ("content_block_delta", "signature_delta"),
        ("content_block_stop", None),
        ("content_block_start", "tool_use"),
        ("content_block_delta", "input_json_delta"),
        ("content_block_delta", "input_json_delta"),
        ("content_block_stop", None),
        ("message_delta", None),
        ("message_stop", None),
    ]
    assert translator.finished and translator.error is None


def test_assembled_message_matches_upstream() -> None:
    out, _ = run(responses_stream_tool_call())
    message = assemble_message(out)
    thinking, tool = message["content"]
    assert thinking["thinking"] == "**Plan**\n\nCreate the file."
    assert decode_reasoning(thinking["signature"]) == {"id": "rs_1", "enc": ENC}
    assert tool == {"type": "tool_use", "id": "call_XYZ", "name": "Bash", "input": {"command": "echo hi > a.txt"}}
    assert message["stop_reason"] == "tool_use"
    assert message["usage"]["input_tokens"] == 200
    assert message["usage"]["cache_read_input_tokens"] == 1000
    assert message["usage"]["output_tokens"] == 80
    assert [d["index"] for n, d in out if n == "content_block_start"] == [0, 1]


def test_tool_arguments_emitted_once_when_only_done_event_carries_them() -> None:
    events = [e for e in responses_stream_tool_call() if e["type"] != "response.function_call_arguments.delta"]
    out, _ = run(events)
    deltas = [
        d["delta"]["partial_json"]
        for n, d in out
        if n == "content_block_delta" and d["delta"]["type"] == "input_json_delta"
    ]
    assert deltas == ['{"command": "echo hi > a.txt"}']


def test_text_stream_and_end_turn() -> None:
    out, _ = run(responses_stream_text(["Hel", "lo", "!"]))
    message = assemble_message(out)
    assert message["content"] == [{"type": "text", "text": "Hello!"}]
    assert message["stop_reason"] == "end_turn"


def test_incomplete_maps_to_max_tokens() -> None:
    out, _ = run(responses_stream_text(["partial"], status="incomplete", reason="max_output_tokens"))
    assert assemble_message(out)["stop_reason"] == "max_tokens"


def test_stop_sequence_cuts_stream() -> None:
    out, _ = run(responses_stream_text(["The answer is 4", "2</ans", "wer> and more"]), stop_sequences=["</answer>"])
    message = assemble_message(out)
    assert message["content"] == [{"type": "text", "text": "The answer is 42"}]
    assert (message["stop_reason"], message["stop_sequence"]) == ("stop_sequence", "</answer>")
    assert [n for n, _ in out][-2:] == ["message_delta", "message_stop"]
    assert sum(1 for n, _ in out if n == "message_stop") == 1


def test_stop_filter_holds_back_partial_prefix() -> None:
    f = StopSequenceFilter(["STOP"])
    assert f.push("abcST") == "ab"
    assert f.push("x") == "c"  # "STx" could still be the start of "STOP"... until it can't
    assert f.push("yz") == "ST"
    assert f.flush() == "xyz"


def test_failed_response_becomes_error_event() -> None:
    events = [
        {"type": "response.created", "response": {"id": "r"}},
        {"type": "response.failed", "response": {"error": {"message": "server exploded"}}},
    ]
    out, translator = run(events)
    assert out[-1][0] == "error"
    assert "server exploded" in out[-1][1]["error"]["message"]
    assert translator.error


def test_truncated_upstream_stream_is_an_error() -> None:
    out, _ = run(responses_stream_text(["abc"])[:-1])
    assert out[-1][0] == "error"


def test_events_are_json_serialisable() -> None:
    out, _ = run(responses_stream_tool_call())
    for _, data in out:
        json.dumps(data)
