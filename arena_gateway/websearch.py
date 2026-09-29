"""Anthropic's web search server tool on top of the provider-hosted search of the Responses API.

Claude Code's ``WebSearch`` tool does not search by itself: it sends a side request to the model with the
Anthropic server tool ``{"type": "web_search_20250305", "name": "web_search", ...}`` and forces it with
``tool_choice: {"type": "tool", "name": "web_search"}``. It then reads the ``server_tool_use`` and
``web_search_tool_result`` blocks (title + url of each hit) and the model's text out of the reply.

The gateway serves such requests with the Responses API's hosted ``web_search`` tool and translates the result:

* ``web_search_call`` (action ``search``) -> ``server_tool_use`` (input ``{"query"}``) + ``web_search_tool_result``
  with one ``web_search_result`` per source URL. Titles come from the model's ``url_citation`` annotations; a
  source that was not cited has no title upstream, so its URL is used as the title.
* ``url_citation`` annotations -> ``web_search_result_location`` citations on the text blocks.
* A failed search becomes a ``web_search_tool_result_error``. If no search succeeded, the model's text is
  **not** returned (it would be written without search results); the client gets the error instead.

The gateway never makes up search results: everything in a ``web_search_tool_result`` is a URL the upstream
search returned.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

RESULT_PREFIX = "arenagw-ws1."  # marks encrypted_content / encrypted_index produced by this gateway
TOOL_NAME = "web_search"


def is_web_search_tool(tool: dict[str, Any]) -> bool:
    """Anthropic web search server tool (``web_search_20250305`` and later versions)."""
    return "input_schema" not in tool and str(tool.get("type") or "").startswith("web_search_")


def _domain(value: Any) -> str:
    """``https://example.com/`` -> ``example.com`` (the Responses API wants bare domains)."""
    text = str(value).strip()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.I)
    return text.rstrip("/")


def convert_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Anthropic ``web_search_*`` server tool -> Responses ``web_search`` hosted tool."""
    out: dict[str, Any] = {"type": "web_search"}
    filters: dict[str, list[str]] = {}
    for key in ("allowed_domains", "blocked_domains"):
        domains = [_domain(d) for d in tool.get(key) or [] if str(d).strip()]
        if domains:
            filters[key] = domains
    if filters:
        out["filters"] = filters
    location = tool.get("user_location")
    if isinstance(location, dict):
        fields = {k: str(location[k]) for k in ("city", "region", "country", "timezone") if location.get(k)}
        if fields:
            out["user_location"] = {"type": "approximate", **fields}
    return out


def clean_url(url: str) -> str:
    """Drop the ``utm_source=openai`` tracking parameter the upstream appends to cited URLs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if "utm_source=openai" not in parts.query:
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if (k, v) != ("utm_source", "openai")]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _url_key(url: str) -> str:
    return clean_url(url).rstrip("/")


def _marker(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return RESULT_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def citation_titles(response: dict[str, Any]) -> dict[str, str]:
    """URL -> title from the ``url_citation`` annotations of all output text."""
    titles: dict[str, str] = {}
    for item in response.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            for ann in part.get("annotations") or []:
                if ann.get("type") == "url_citation" and ann.get("url") and ann.get("title"):
                    titles.setdefault(_url_key(ann["url"]), str(ann["title"]))
    return titles


def search_query(action: dict[str, Any]) -> str:
    query = action.get("query")
    if not query and action.get("queries"):
        query = " | ".join(str(q) for q in action["queries"] if q)
    return str(query or "")


def server_tool_use_id(item: dict[str, Any], index: int) -> str:
    raw = re.sub(r"[^A-Za-z0-9]", "", str(item.get("id") or ""))
    return "srvtoolu_" + (raw[-24:] if raw else f"arena{index:04d}")


def call_blocks(
    item: dict[str, Any], titles: dict[str, str], index: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One ``web_search_call`` item -> Anthropic blocks + a summary for the request log.

    Only ``search`` actions map onto Anthropic's web search tool; ``open_page`` / ``find_in_page`` actions
    (the model reading a page it found) produce no blocks but are counted."""
    action = item.get("action") or {}
    kind = action.get("type") or "search"
    ok = item.get("status") == "completed"
    summary: dict[str, Any] = {"action": kind, "status": item.get("status")}
    if kind != "search":
        if action.get("url"):
            summary["url"] = action["url"]
        return [], summary

    query = search_query(action)
    tool_id = server_tool_use_id(item, index)
    use = {"type": "server_tool_use", "id": tool_id, "name": TOOL_NAME, "input": {"query": query}}
    content: Any
    if not ok:
        content = {"type": "web_search_tool_result_error", "error_code": "unavailable"}
        summary.update(query=query, sources=0)
    else:
        content = []
        seen: set[str] = set()
        for source in action.get("sources") or []:
            url = source.get("url") if isinstance(source, dict) else None
            if not url:
                continue
            key = _url_key(str(url))
            if key in seen:
                continue
            seen.add(key)
            clean = clean_url(str(url))
            content.append(
                {
                    "type": "web_search_result",
                    "url": clean,
                    "title": titles.get(key) or clean,
                    "encrypted_content": _marker({"url": clean}),
                    "page_age": None,
                }
            )
        summary.update(query=query, sources=len(content))
    result = {"type": "web_search_tool_result", "tool_use_id": tool_id, "content": content}
    return [use, result], summary


def cited_text_blocks(text: str, annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split output text at ``url_citation`` spans; cited spans carry ``web_search_result_location`` citations."""
    spans = sorted(
        (
            (int(a["start_index"]), int(a["end_index"]), a)
            for a in annotations
            if a.get("type") == "url_citation" and a.get("url") and isinstance(a.get("start_index"), int)
        ),
        key=lambda s: (s[0], s[1]),
    )
    blocks: list[dict[str, Any]] = []
    pos = 0
    i = 0
    while i < len(spans):
        start, end, _ = spans[i]
        group = [spans[i]]
        i += 1
        while i < len(spans) and spans[i][0] < end:  # overlapping citations share one block
            end = max(end, spans[i][1])
            group.append(spans[i])
            i += 1
        start, end = max(start, pos), min(end, len(text))
        if start >= end:
            continue
        if start > pos:
            blocks.append({"type": "text", "text": text[pos:start]})
        cited = text[start:end]
        citations = []
        for _, _, ann in group:
            url = clean_url(str(ann["url"]))
            citations.append(
                {
                    "type": "web_search_result_location",
                    "url": url,
                    "title": str(ann.get("title") or url),
                    "encrypted_index": _marker({"url": url}),
                    "cited_text": cited[:150],
                }
            )
        blocks.append({"type": "text", "text": cited, "citations": citations})
        pos = end
    if pos < len(text):
        blocks.append({"type": "text", "text": text[pos:]})
    return [b for b in blocks if b["text"]]


def failure_blocks(index: int) -> list[dict[str, Any]]:
    """What the client gets when the model was required to search and no search succeeded."""
    tool_id = f"srvtoolu_arenanosearch{index:04d}"
    return [
        {"type": "server_tool_use", "id": tool_id, "name": TOOL_NAME, "input": {}},
        {
            "type": "web_search_tool_result",
            "tool_use_id": tool_id,
            "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"},
        },
    ]


def history_text(block: dict[str, Any]) -> str | None:
    """Earlier ``server_tool_use`` / ``web_search_tool_result`` blocks, as assistant text for the upstream."""
    kind = block.get("type")
    if kind == "server_tool_use":
        query = (block.get("input") or {}).get("query")
        return f"[web search: {query}]\n" if query else "[web search]\n"
    if kind == "web_search_tool_result":
        content = block.get("content")
        if not isinstance(content, list):
            code = (content or {}).get("error_code") if isinstance(content, dict) else None
            return f"[web search error: {code or 'unknown'}]\n"
        if not content:
            return "[web search: no results]\n"
        lines = [f"- {r.get('title') or r.get('url')} <{r.get('url')}>" for r in content if isinstance(r, dict)]
        return "[web search results]\n" + "\n".join(lines) + "\n"
    return None
