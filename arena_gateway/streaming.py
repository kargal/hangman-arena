"""Streaming translation: OpenAI Responses SSE events -> Anthropic Messages SSE events.

Anthropic event sequence produced for one response::

    message_start
    ping
    content_block_start / content_block_delta* / content_block_stop   (per block)
        thinking : thinking_delta* then signature_delta
        text     : text_delta*
        tool_use : input_json_delta*
    message_delta   (stop_reason, stop_sequence, usage)
    message_stop

The translator is a pure state machine (``feed`` one upstream event, get back a list of
Anthropic events), which keeps it easy to test without a network.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from arena_gateway.signature import encode_reasoning
from arena_gateway.translate import RequestContext, convert_usage, new_message_id, stop_reason_for

Event = tuple[str, dict[str, Any]]


def format_sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Parse an SSE line stream into JSON payloads (``data: [DONE]`` is ignored)."""
    data_lines: list[str] = []
    async for line in lines:
        if line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                data_lines = []
                if raw.strip() and raw.strip() != "[DONE]":
                    yield json.loads(raw)
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if data_lines:
        raw = "\n".join(data_lines)
        if raw.strip() and raw.strip() != "[DONE]":
            yield json.loads(raw)


class StopSequenceFilter:
    """Hold back text that might be the start of a stop sequence; cut at the first match."""

    def __init__(self, stops: list[str]) -> None:
        self.stops = [s for s in stops if s]
        self.keep = max((len(s) for s in self.stops), default=1) - 1
        self.buffer = ""
        self.hit: str | None = None

    def push(self, text: str) -> str:
        if not self.stops:
            return text
        self.buffer += text
        best: tuple[int, str] | None = None
        for seq in self.stops:
            idx = self.buffer.find(seq)
            if idx != -1 and (best is None or idx < best[0]):
                best = (idx, seq)
        if best is not None:
            out = self.buffer[: best[0]]
            self.buffer = ""
            self.hit = best[1]
            return out
        if self.keep <= 0:
            out, self.buffer = self.buffer, ""
            return out
        out, self.buffer = self.buffer[: -self.keep], self.buffer[-self.keep :]
        return out

    def flush(self) -> str:
        out, self.buffer = self.buffer, ""
        return out


@dataclass
class _Block:
    index: int
    kind: str  # thinking | text | tool_use
    open: bool = True
    args_emitted: bool = False
    summary_parts: int = 0


@dataclass
class StreamTranslator:
    ctx: RequestContext
    message_id: str = field(default_factory=new_message_id)
    blocks: dict[str, _Block] = field(default_factory=dict)
    next_index: int = 0
    saw_tool_call: bool = False
    finished: bool = False
    upstream_response: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        self.stop_filter = StopSequenceFilter(self.ctx.stop_sequences)

    # -- framing -------------------------------------------------------------

    def start(self) -> list[Event]:
        return [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.ctx.client_model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": self.ctx.estimated_input_tokens,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "output_tokens": 1,
                        },
                    },
                },
            ),
            ("ping", {"type": "ping"}),
        ]

    def _open(self, key: str, kind: str, content_block: dict[str, Any]) -> list[Event]:
        block = _Block(index=self.next_index, kind=kind)
        self.next_index += 1
        self.blocks[key] = block
        return [
            (
                "content_block_start",
                {"type": "content_block_start", "index": block.index, "content_block": content_block},
            )
        ]

    def _delta(self, block: _Block, delta: dict[str, Any]) -> Event:
        return ("content_block_delta", {"type": "content_block_delta", "index": block.index, "delta": delta})

    def _close(self, block: _Block) -> list[Event]:
        if not block.open:
            return []
        block.open = False
        return [("content_block_stop", {"type": "content_block_stop", "index": block.index})]

    def _close_all(self) -> list[Event]:
        events: list[Event] = []
        for block in sorted(self.blocks.values(), key=lambda b: b.index):
            if block.kind == "text" and block.open:
                tail = self.stop_filter.flush() if self.stop_filter.hit is None else ""
                if tail:
                    events.append(self._delta(block, {"type": "text_delta", "text": tail}))
            events.extend(self._close(block))
        return events

    def _finish(self, response: dict[str, Any]) -> list[Event]:
        self.finished = True
        self.upstream_response = response
        events = self._close_all()
        if self.stop_filter.hit is not None:
            self.stop_reason = "stop_sequence"
        else:
            self.stop_reason = stop_reason_for(response, self.saw_tool_call)
        events.append(
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": self.stop_reason, "stop_sequence": self.stop_filter.hit},
                    "usage": convert_usage(response.get("usage")),
                },
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        return events

    def fail(self, message: str, kind: str = "api_error") -> list[Event]:
        self.finished = True
        self.error = message
        return [("error", {"type": "error", "error": {"type": kind, "message": message}})]

    # -- upstream events --------------------------------------------------------

    def feed(self, event: dict[str, Any]) -> list[Event]:
        if self.finished:
            return []
        etype = event.get("type", "")
        handler = getattr(self, "_on_" + etype.replace(".", "_"), None)
        return handler(event) if handler else []

    def _key(self, event: dict[str, Any]) -> str:
        item = event.get("item") or {}
        return str(event.get("item_id") or item.get("id") or event.get("output_index"))

    def _on_response_output_item_added(self, event: dict[str, Any]) -> list[Event]:
        item = event.get("item") or {}
        key = self._key(event)
        kind = item.get("type")
        if kind == "reasoning":
            return self._open(key, "thinking", {"type": "thinking", "thinking": "", "signature": ""})
        if kind == "function_call":
            self.saw_tool_call = True
            return self._open(
                key, "tool_use", {"type": "tool_use", "id": item.get("call_id"), "name": item.get("name"), "input": {}}
            )
        return []  # message blocks are opened lazily on the first text delta

    # reasoning ----------------------------------------------------------------

    def _on_response_reasoning_summary_part_added(self, event: dict[str, Any]) -> list[Event]:
        block = self.blocks.get(self._key(event))
        if block is None:
            return []
        block.summary_parts += 1
        if block.summary_parts > 1:
            return [self._delta(block, {"type": "thinking_delta", "thinking": "\n\n"})]
        return []

    def _on_response_reasoning_summary_text_delta(self, event: dict[str, Any]) -> list[Event]:
        block = self.blocks.get(self._key(event))
        if block is None or not event.get("delta"):
            return []
        return [self._delta(block, {"type": "thinking_delta", "thinking": event["delta"]})]

    _on_response_reasoning_text_delta = _on_response_reasoning_summary_text_delta

    # text -----------------------------------------------------------------------

    def _text(self, event: dict[str, Any]) -> list[Event]:
        if self.stop_filter.hit is not None:
            return []
        key = self._key(event)
        events: list[Event] = []
        text = self.stop_filter.push(event.get("delta") or "")
        block = self.blocks.get(key)
        if text and block is None:
            events += self._open(key, "text", {"type": "text", "text": ""})
            block = self.blocks[key]
        if text and block is not None:
            events.append(self._delta(block, {"type": "text_delta", "text": text}))
        if self.stop_filter.hit is not None:
            events += self._finish({"status": "completed", "usage": {}})
        return events

    _on_response_output_text_delta = _text
    _on_response_refusal_delta = _text

    # tool calls -----------------------------------------------------------------

    def _on_response_function_call_arguments_delta(self, event: dict[str, Any]) -> list[Event]:
        block = self.blocks.get(self._key(event))
        if block is None or not event.get("delta"):
            return []
        block.args_emitted = True
        return [self._delta(block, {"type": "input_json_delta", "partial_json": event["delta"]})]

    def _emit_full_arguments(self, block: _Block, arguments: str | None) -> list[Event]:
        if block.args_emitted or not arguments:
            return []
        block.args_emitted = True
        return [self._delta(block, {"type": "input_json_delta", "partial_json": arguments})]

    def _on_response_function_call_arguments_done(self, event: dict[str, Any]) -> list[Event]:
        block = self.blocks.get(self._key(event))
        return self._emit_full_arguments(block, event.get("arguments")) if block else []

    # item completion ------------------------------------------------------------

    def _on_response_output_item_done(self, event: dict[str, Any]) -> list[Event]:
        item = event.get("item") or {}
        block = self.blocks.get(self._key(event))
        if block is None:
            return []
        events: list[Event] = []
        if block.kind == "thinking":
            if not block.summary_parts:
                summary = "\n\n".join(s.get("text", "") for s in item.get("summary") or [] if s.get("text"))
                if summary:
                    events.append(self._delta(block, {"type": "thinking_delta", "thinking": summary}))
            events.append(self._delta(block, {"type": "signature_delta", "signature": encode_reasoning(item)}))
        elif block.kind == "tool_use":
            events += self._emit_full_arguments(block, item.get("arguments"))
        elif block.kind == "text":
            tail = self.stop_filter.flush()
            if tail:
                events.append(self._delta(block, {"type": "text_delta", "text": tail}))
        return events + self._close(block)

    # response completion -------------------------------------------------------------

    def _on_response_completed(self, event: dict[str, Any]) -> list[Event]:
        return self._finish(event.get("response") or {})

    _on_response_incomplete = _on_response_completed

    def _on_response_failed(self, event: dict[str, Any]) -> list[Event]:
        err = (event.get("response") or {}).get("error") or {}
        return self.fail(f"upstream response failed: {err.get('message') or err or 'unknown error'}")

    def _on_error(self, event: dict[str, Any]) -> list[Event]:
        message = event.get("message") or (event.get("error") or {}).get("message") or "upstream error"
        code = event.get("code") or (event.get("error") or {}).get("code")
        kind = "overloaded_error" if code in ("server_is_overloaded", "slow_down") else "api_error"
        return self.fail(f"upstream error: {message}", kind)

    def end_of_stream(self) -> list[Event]:
        """Upstream closed the connection. Emit an error if no terminal event was seen."""
        if self.finished:
            return []
        return self.fail("upstream stream ended unexpectedly", "api_error")
