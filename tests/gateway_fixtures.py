"""Recorded-shape OpenAI Responses streaming events used by the gateway tests."""

from __future__ import annotations

import json
from typing import Any

ENC = "gAAAAABo-encrypted-reasoning-state=="


def responses_stream_tool_call() -> list[dict[str, Any]]:
    """reasoning (2 summary parts) -> function_call with streamed arguments -> completed."""
    resp = {"id": "resp_abc", "object": "response", "status": "in_progress", "output": []}
    reasoning_done = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "**Plan**"}, {"type": "summary_text", "text": "Create the file."}],
        "encrypted_content": ENC,
    }
    call_done = {
        "id": "fc_1",
        "type": "function_call",
        "status": "completed",
        "call_id": "call_XYZ",
        "name": "Bash",
        "arguments": '{"command": "echo hi > a.txt"}',
    }
    return [
        {"type": "response.created", "sequence_number": 0, "response": resp},
        {"type": "response.in_progress", "sequence_number": 1, "response": resp},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "rs_1", "type": "reasoning", "summary": []},
        },
        {"type": "response.reasoning_summary_part.added", "item_id": "rs_1", "output_index": 0, "summary_index": 0},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "summary_index": 0, "delta": "**Plan**"},
        {"type": "response.reasoning_summary_part.done", "item_id": "rs_1", "summary_index": 0},
        {"type": "response.reasoning_summary_part.added", "item_id": "rs_1", "output_index": 0, "summary_index": 1},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "summary_index": 1, "delta": "Create "},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "summary_index": 1, "delta": "the file."},
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning_done},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {"id": "fc_1", "type": "function_call", "call_id": "call_XYZ", "name": "Bash", "arguments": ""},
        },
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 1, "delta": '{"command"'},
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "output_index": 1,
            "delta": ': "echo hi > a.txt"}',
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_1",
            "output_index": 1,
            "arguments": call_done["arguments"],
        },
        {"type": "response.output_item.done", "output_index": 1, "item": call_done},
        {
            "type": "response.completed",
            "response": {
                **resp,
                "status": "completed",
                "output": [reasoning_done, call_done],
                "usage": {
                    "input_tokens": 1200,
                    "input_tokens_details": {"cached_tokens": 1000},
                    "output_tokens": 80,
                    "output_tokens_details": {"reasoning_tokens": 50},
                },
            },
        },
    ]


def responses_stream_text(
    text_chunks: list[str], *, status: str = "completed", reason: str | None = None
) -> list[dict[str, Any]]:
    msg_id = "msg_up_1"
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {"id": "resp_t", "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": msg_id, "type": "message", "role": "assistant", "content": []},
        },
        {
            "type": "response.content_part.added",
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
    ]
    for chunk in text_chunks:
        events.append(
            {
                "type": "response.output_text.delta",
                "item_id": msg_id,
                "output_index": 0,
                "content_index": 0,
                "delta": chunk,
            }
        )
    full = "".join(text_chunks)
    item = {"id": msg_id, "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": full}]}
    events.append({"type": "response.output_text.done", "item_id": msg_id, "text": full})
    events.append({"type": "response.output_item.done", "output_index": 0, "item": item})
    terminal = "response.completed" if status == "completed" else "response.incomplete"
    events.append(
        {
            "type": terminal,
            "response": {
                "id": "resp_t",
                "status": status,
                "incomplete_details": {"reason": reason} if reason else None,
                "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }
    )
    return events


def to_sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def parse_anthropic_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for chunk in raw.strip().split("\n\n"):
        lines = chunk.splitlines()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        assert data["type"] == event
        out.append((event, data))
    return out


def assemble_message(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Rebuild the final message the way an Anthropic SDK client would."""
    message: dict[str, Any] = {}
    partial_json: dict[int, str] = {}
    for name, data in events:
        if name == "message_start":
            message = dict(data["message"])
            message["content"] = []
        elif name == "content_block_start":
            message["content"].append(dict(data["content_block"]))
        elif name == "content_block_delta":
            block = message["content"][data["index"]]
            delta = data["delta"]
            if delta["type"] == "text_delta":
                block["text"] += delta["text"]
            elif delta["type"] == "thinking_delta":
                block["thinking"] += delta["thinking"]
            elif delta["type"] == "signature_delta":
                block["signature"] = delta["signature"]
            elif delta["type"] == "input_json_delta":
                partial_json[data["index"]] = partial_json.get(data["index"], "") + delta["partial_json"]
        elif name == "content_block_stop":
            if data["index"] in partial_json:
                message["content"][data["index"]]["input"] = json.loads(partial_json[data["index"]])
        elif name == "message_delta":
            message.update(data["delta"])
            message["usage"] = {**message.get("usage", {}), **data["usage"]}
    return message
