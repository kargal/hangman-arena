"""Carrying OpenAI reasoning state through Anthropic ``thinking`` block signatures.

Requests are sent with ``store=false``, so the upstream keeps no state between
turns. Each reasoning item comes back with ``encrypted_content``; the gateway
packs it (and the item id) into the ``signature`` of the ``thinking`` block it
emits. Claude Code returns thinking blocks verbatim on later turns, and the
gateway unpacks them into reasoning input items so the model keeps its
chain of thought across tool calls.
"""

from __future__ import annotations

import base64
import json
from typing import Any

PREFIX = "arenagw1."


def encode_reasoning(item: dict[str, Any]) -> str:
    payload = {"id": item.get("id"), "enc": item.get("encrypted_content")}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_reasoning(signature: str | None) -> dict[str, Any] | None:
    """Return ``{"id", "enc"}`` for signatures produced by this gateway, else ``None``."""
    if not signature or not signature.startswith(PREFIX):
        return None
    body = signature[len(PREFIX) :]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("enc"):
        return None
    return {"id": data.get("id"), "enc": data["enc"]}
