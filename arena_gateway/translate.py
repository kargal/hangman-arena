"""Request/response translation: Anthropic Messages <-> OpenAI Responses."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from typing import Any

from arena_gateway import websearch
from arena_gateway.config import GatewayConfig, ModelRoute
from arena_gateway.signature import decode_reasoning, encode_reasoning

log = logging.getLogger(__name__)

BILLING_HEADER_PREFIX = "x-anthropic-billing-header:"


@dataclass
class RequestContext:
    """What the response side needs to know about the original request."""

    request_id: str
    client_model: str
    route: ModelRoute
    stream: bool
    stop_sequences: list[str] = field(default_factory=list)
    estimated_input_tokens: int = 0
    dropped: list[str] = field(default_factory=list)  # content/tool types we could not translate
    web_search_tools: list[str] = field(default_factory=list)  # names of Anthropic web search server tools
    web_search_forced: bool = False  # tool_choice requires the web search tool
    web_search: dict[str, Any] | None = None  # filled by convert_response, written to the request log

    @property
    def wants_web_search(self) -> bool:
        return bool(self.web_search_tools)


def new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


# --------------------------------------------------------------------------- token estimate


def estimate_tokens(body: dict[str, Any]) -> int:
    """Rough token count (~3.8 characters per token) of system + messages + tools."""
    parts = [body.get("system") or "", body.get("messages") or [], body.get("tools") or []]
    chars = sum(len(p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)) for p in parts)
    return max(1, math.ceil(chars / 3.8))


# --------------------------------------------------------------------------- request


def _text_of_system(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    texts = []
    for block in system:
        text = block.get("text", "") if isinstance(block, dict) else str(block)
        if text.startswith(BILLING_HEADER_PREFIX):
            continue
        texts.append(text)
    return "\n\n".join(t for t in texts if t)


def _blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return [b for b in content if isinstance(b, dict)]


def _image_part(block: dict[str, Any]) -> dict[str, Any] | None:
    source = block.get("source") or {}
    if source.get("type") == "base64":
        return {"type": "input_image", "image_url": f"data:{source.get('media_type')};base64,{source.get('data')}"}
    if source.get("type") == "url":
        return {"type": "input_image", "image_url": source.get("url")}
    return None


def _document_part(block: dict[str, Any]) -> dict[str, Any] | None:
    source = block.get("source") or {}
    if source.get("type") == "text":
        return {"type": "input_text", "text": source.get("data", "")}
    if source.get("type") == "base64":
        return {
            "type": "input_file",
            "filename": block.get("title") or "document.pdf",
            "file_data": f"data:{source.get('media_type', 'application/pdf')};base64,{source.get('data')}",
        }
    if source.get("type") == "url":
        return {"type": "input_file", "file_url": source.get("url")}
    return None


def _tool_result_items(block: dict[str, Any], ctx: RequestContext) -> list[dict[str, Any]]:
    """A tool_result becomes a function_call_output (+ a user message carrying any images)."""
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for part in _blocks(block.get("content")):
        kind = part.get("type")
        if kind == "text":
            texts.append(part.get("text", ""))
        elif kind == "image":
            img = _image_part(part)
            if img:
                images.append(img)
                texts.append("[image attached below]")
        elif kind == "tool_reference":
            texts.append(f"[tool loaded: {part.get('tool_name') or part.get('name')}]")
        elif kind == "search_result":
            body = "\n".join(c.get("text", "") for c in _blocks(part.get("content")))
            texts.append(f"{part.get('title', '')}\n{part.get('source', '')}\n{body}".strip())
        else:
            ctx.dropped.append(f"tool_result.{kind}")
    output = "\n".join(texts)
    if block.get("is_error"):
        output = f"Error: {output}" if output else "Error"
    items: list[dict[str, Any]] = [
        {"type": "function_call_output", "call_id": block.get("tool_use_id"), "output": output}
    ]
    if images:
        items.append({"type": "message", "role": "user", "content": images})
    return items


def convert_messages(messages: list[dict[str, Any]], ctx: RequestContext) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        blocks = _blocks(message.get("content"))

        if role in ("user", "system"):
            target_role = "user" if role == "user" else "developer"
            parts: list[dict[str, Any]] = []

            def flush(parts: list[dict[str, Any]] = parts, target_role: str = target_role) -> None:
                if parts:
                    items.append({"type": "message", "role": target_role, "content": list(parts)})
                    parts.clear()

            for block in blocks:
                kind = block.get("type")
                if kind == "text":
                    if block.get("text"):
                        parts.append({"type": "input_text", "text": block["text"]})
                elif kind == "image":
                    img = _image_part(block)
                    if img:
                        parts.append(img)
                elif kind == "document":
                    doc = _document_part(block)
                    if doc:
                        parts.append(doc)
                elif kind == "tool_result":
                    flush()
                    items.extend(_tool_result_items(block, ctx))
                else:
                    ctx.dropped.append(f"{role}.{kind}")
            flush()

        elif role == "assistant":
            texts: list[str] = []

            def flush_text(texts: list[str] = texts) -> None:
                if texts:
                    items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "".join(texts)}],
                        }
                    )
                    texts.clear()

            for block in blocks:
                kind = block.get("type")
                if kind == "text":
                    if block.get("text"):
                        texts.append(block["text"])
                elif kind in ("thinking", "redacted_thinking"):
                    state = decode_reasoning(block.get("signature") or block.get("data"))
                    if state is None:
                        ctx.dropped.append(f"assistant.{kind}(foreign signature)")
                        continue
                    flush_text()
                    summary = block.get("thinking") or ""
                    item: dict[str, Any] = {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": summary}] if summary else [],
                        "encrypted_content": state["enc"],
                    }
                    if state.get("id"):
                        item["id"] = state["id"]
                    items.append(item)
                elif kind in ("server_tool_use", "web_search_tool_result"):
                    note = websearch.history_text(block)
                    if note:
                        texts.append(note)
                elif kind == "tool_use":
                    flush_text()
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": block.get("id"),
                            "name": block.get("name"),
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        }
                    )
                else:
                    ctx.dropped.append(f"assistant.{kind}")
            flush_text()
        else:
            ctx.dropped.append(f"role.{role}")
    return items


def clean_schema(schema: Any) -> Any:
    """Drop JSON-Schema keys the upstream does not need (``$schema``); keep everything else."""
    if isinstance(schema, dict):
        return {k: clean_schema(v) for k, v in schema.items() if k != "$schema"}
    if isinstance(schema, list):
        return [clean_schema(v) for v in schema]
    return schema


def convert_tools(tools: list[dict[str, Any]], ctx: RequestContext) -> list[dict[str, Any]]:
    out = []
    for tool in tools or []:
        if websearch.is_web_search_tool(tool):  # served by the provider-hosted search (see websearch.py)
            ctx.web_search_tools.append(str(tool.get("name") or websearch.TOOL_NAME))
            out.append(websearch.convert_tool(tool))
            continue
        if "input_schema" not in tool:  # other Anthropic server tools (code execution, web fetch, ...)
            ctx.dropped.append(f"tool.{tool.get('type') or tool.get('name')}")
            continue
        if tool.get("defer_loading"):  # not visible to the model until loaded via ToolSearch
            continue
        schema = clean_schema(tool["input_schema"]) or {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        if schema.get("type") == "object":
            schema.setdefault("properties", {})
        out.append(
            {
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": schema,
                "strict": False,
            }
        )
    return out


def convert_tool_choice(choice: dict[str, Any] | None) -> tuple[Any, bool | None]:
    """Return ``(tool_choice, parallel_tool_calls)``."""
    if not choice:
        return None, None
    parallel = False if choice.get("disable_parallel_tool_use") else None
    kind = choice.get("type")
    if kind == "auto":
        return "auto", parallel
    if kind == "any":
        return "required", parallel
    if kind == "none":
        return "none", parallel
    if kind == "tool":
        return {"type": "function", "name": choice.get("name")}, parallel
    return None, parallel


def _session_key(body: dict[str, Any], headers: dict[str, str]) -> str | None:
    session = headers.get("x-claude-code-session-id")
    user_id = (body.get("metadata") or {}).get("user_id")
    if not session and isinstance(user_id, str):
        try:
            session = json.loads(user_id).get("session_id")
        except (ValueError, AttributeError):
            session = user_id
    if not session:
        return None
    return "arena-" + hashlib.sha256(str(session).encode()).hexdigest()[:32]


def build_upstream_request(
    body: dict[str, Any], config: GatewayConfig, headers: dict[str, str] | None = None
) -> tuple[dict[str, Any], RequestContext]:
    """Translate an Anthropic ``/v1/messages`` body into an OpenAI ``/responses`` payload."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    route = config.resolve(body.get("model"))
    ctx = RequestContext(
        request_id=uuid.uuid4().hex[:16],
        client_model=str(body.get("model")),
        route=route,
        stream=bool(body.get("stream")),
        stop_sequences=[s for s in body.get("stop_sequences") or [] if s],
        estimated_input_tokens=estimate_tokens(body),
    )

    client_max = int(body.get("max_tokens") or 0)
    floor = config.max_output_tokens_floor
    if route.max_output_tokens:
        floor = min(floor, route.max_output_tokens)
    payload: dict[str, Any] = {
        "model": route.upstream_model,
        "input": convert_messages(body.get("messages") or [], ctx),
        "store": False,
        "stream": ctx.stream,
        "max_output_tokens": max(client_max, floor),
    }
    instructions = _text_of_system(body.get("system"))
    if instructions:
        payload["instructions"] = instructions

    if route.reasoning:
        reasoning: dict[str, Any] = {}
        if route.reasoning_effort:
            reasoning["effort"] = route.reasoning_effort
        if route.reasoning_summary:
            reasoning["summary"] = route.reasoning_summary
        payload["reasoning"] = reasoning
        payload["include"] = ["reasoning.encrypted_content"]
        # Claude Code's `thinking` / `output_config.effort` are ignored: effort comes from models.yaml,
        # and reasoning models reject sampling parameters.
    else:
        for key in ("temperature", "top_p"):
            if body.get(key) is not None:
                payload[key] = body[key]

    tools = convert_tools(body.get("tools") or [], ctx)
    if tools:
        payload["tools"] = tools
        choice, parallel = convert_tool_choice(body.get("tool_choice"))
        only_search = len(tools) == len(ctx.web_search_tools)
        if (isinstance(choice, dict) and choice.get("name") in ctx.web_search_tools) or (
            ctx.web_search_tools and only_search and choice in (None, "auto")
        ):
            # Forced web search, or a request whose only tool is web search (Claude Code's WebSearch side
            # request, sent with tool_choice auto): require the hosted search.
            choice = {"type": "web_search"}
            ctx.web_search_forced = True
        if choice is not None:
            payload["tool_choice"] = choice
        if parallel is not None:
            payload["parallel_tool_calls"] = parallel
    if ctx.wants_web_search:
        # Served from one complete upstream response (titles come from the final text's citations).
        payload["stream"] = False
        payload["include"] = [*payload.get("include", []), "web_search_call.action.sources"]
        max_uses = [
            int(t["max_uses"]) for t in body.get("tools") or [] if websearch.is_web_search_tool(t) and t.get("max_uses")
        ]
        if max_uses:
            payload["max_tool_calls"] = min(max_uses)

    if route.provider.prompt_cache_key:
        cache_key = _session_key(body, headers)
        if cache_key:
            payload["prompt_cache_key"] = cache_key

    if ctx.dropped:
        log.info("request %s: untranslated content %s", ctx.request_id, sorted(set(ctx.dropped)))
    return payload, ctx


# --------------------------------------------------------------------------- response


def _usage_parts(usage: dict[str, Any] | None) -> tuple[int, int, int, int]:
    """``(uncached_input, cache_read, cache_write, output)``. On GPT-5.6+ ``input_tokens`` includes
    ``cached_tokens`` (reads) and ``cache_write_tokens`` (writes, billed at 1.25x); they do not overlap."""
    usage = usage or {}
    details = usage.get("input_tokens_details") or {}
    total_in = int(usage.get("input_tokens") or 0)
    cached = int(details.get("cached_tokens") or 0)
    written = int(details.get("cache_write_tokens") or 0)
    return max(0, total_in - cached - written), cached, written, int(usage.get("output_tokens") or 0)


def convert_usage(usage: dict[str, Any] | None) -> dict[str, Any]:
    uncached, cached, written, output = _usage_parts(usage)
    return {
        "input_tokens": uncached,
        "cache_creation_input_tokens": written,
        "cache_read_input_tokens": cached,
        "output_tokens": output,
    }


def log_usage(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Usage as recorded in the request log (OpenAI semantics: ``input_tokens`` is the total)."""
    usage = usage or {}
    _, cached, written, output = _usage_parts(usage)
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_input_tokens": cached,
        "cache_write_tokens": written,
        "output_tokens": output,
    }


def reasoning_tokens(usage: dict[str, Any] | None) -> int:
    return int(((usage or {}).get("output_tokens_details") or {}).get("reasoning_tokens") or 0)


def stop_reason_for(response: dict[str, Any], saw_tool_call: bool) -> str:
    if response.get("status") == "incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason")
        if reason == "max_output_tokens":
            return "max_tokens"
        if reason == "content_filter":
            return "refusal"
    if saw_tool_call:
        return "tool_use"
    return "end_turn"


def _summary_text(item: dict[str, Any]) -> str:
    return "\n\n".join(s.get("text", "") for s in item.get("summary") or [] if s.get("text"))


def parse_arguments(arguments: str | None) -> Any:
    if not arguments:
        return {}
    try:
        value = json.loads(arguments)
    except ValueError:
        return {"_raw_arguments": arguments}
    return value if isinstance(value, dict) else {"value": value}


def convert_response(response: dict[str, Any], ctx: RequestContext) -> dict[str, Any]:
    """Translate a complete (non-streaming) Responses object into an Anthropic message."""
    content: list[dict[str, Any]] = []
    saw_tool_call = False
    stop_hit: str | None = None
    titles = websearch.citation_titles(response) if ctx.wants_web_search else {}
    calls: list[dict[str, Any]] = []
    for item in response.get("output") or []:
        kind = item.get("type")
        if stop_hit:
            break
        if kind == "reasoning":
            content.append({"type": "thinking", "thinking": _summary_text(item), "signature": encode_reasoning(item)})
        elif kind == "web_search_call":
            blocks, summary = websearch.call_blocks(item, titles, len(calls))
            calls.append(summary)
            content.extend(blocks)
        elif kind == "message" and ctx.wants_web_search and not ctx.stop_sequences:
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    content.extend(websearch.cited_text_blocks(part.get("text") or "", part.get("annotations") or []))
                elif part.get("type") == "refusal" and part.get("refusal"):
                    content.append({"type": "text", "text": part["refusal"]})
        elif kind == "message":
            text = "".join(
                part.get("text") or part.get("refusal") or ""
                for part in item.get("content") or []
                if part.get("type") in ("output_text", "refusal")
            )
            for seq in ctx.stop_sequences:
                idx = text.find(seq)
                if idx != -1 and (stop_hit is None or idx < text.find(stop_hit)):
                    stop_hit = seq
            if stop_hit:
                text = text[: text.find(stop_hit)]
            if text:
                content.append({"type": "text", "text": text})
        elif kind == "function_call":
            saw_tool_call = True
            content.append(
                {
                    "type": "tool_use",
                    "id": item.get("call_id"),
                    "name": item.get("name"),
                    "input": parse_arguments(item.get("arguments")),
                }
            )
    usage = convert_usage(response.get("usage"))
    if ctx.wants_web_search:
        content = _check_web_search(content, calls, ctx)
        usage["server_tool_use"] = {"web_search_requests": ctx.web_search["searches"] if ctx.web_search else 0}
    stop_reason = "stop_sequence" if stop_hit else stop_reason_for(response, saw_tool_call)
    return {
        "id": new_message_id(),
        "type": "message",
        "role": "assistant",
        "model": ctx.client_model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": stop_hit,
        "usage": usage,
    }


def _check_web_search(
    content: list[dict[str, Any]], calls: list[dict[str, Any]], ctx: RequestContext
) -> list[dict[str, Any]]:
    """Record what was searched; never pass on model text written without any successful search when a search
    was required or attempted -- return a web search error result instead."""
    searches = [c for c in calls if c["action"] == "search"]
    succeeded = [c for c in searches if c.get("status") == "completed"]
    suppressed = bool((ctx.web_search_forced or searches) and not succeeded)
    if suppressed:
        content = [b for b in content if b["type"] not in ("text",)]
        if not any(b["type"] == "web_search_tool_result" for b in content):
            content.extend(websearch.failure_blocks(len(calls)))
    ctx.web_search = {
        "calls": len(calls),
        "searches": len(searches),
        "failed": len(searches) - len(succeeded),
        "sources": sum(int(c.get("sources") or 0) for c in searches),
        "queries": [c.get("query") for c in searches],
        "other_actions": [c["action"] for c in calls if c["action"] != "search"],
        "text_suppressed": suppressed,
    }
    return content


# --------------------------------------------------------------------------- errors

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    503: "overloaded_error",
    529: "overloaded_error",
}


def anthropic_error(status: int, message: str) -> dict[str, Any]:
    kind = _ERROR_TYPES.get(status, "api_error" if status >= 500 else "invalid_request_error")
    return {"type": "error", "error": {"type": kind, "message": message}}


def upstream_error_message(status: int, body: Any) -> str:
    """Human-readable message; context-length errors are phrased the way Claude Code expects
    (``prompt is too long``) so that it compacts the conversation and retries."""
    err = body.get("error") if isinstance(body, dict) else None
    message = (err or {}).get("message") if isinstance(err, dict) else None
    code = (err or {}).get("code") if isinstance(err, dict) else None
    message = message or (body if isinstance(body, str) else json.dumps(body))[:500]
    if code == "context_length_exceeded" or "context window" in str(message).lower():
        return f"prompt is too long: {message}"
    return f"upstream error {status}: {message}"
