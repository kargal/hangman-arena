"""Agent adapters, keyed by the ``adapter`` field of an agent config."""

from __future__ import annotations

from arena_runner.adapters.base import Adapter, AttemptResult
from arena_runner.adapters.claude_code import ClaudeCodeAdapter

ADAPTERS: dict[str, type[Adapter]] = {cls.name: cls for cls in (ClaudeCodeAdapter)}


def get_adapter(name: str) -> type[Adapter]:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise KeyError(f"unknown adapter {name!r}; available: {', '.join(sorted(ADAPTERS))}") from None


__all__ = ["ADAPTERS", "Adapter", "AttemptResult", "get_adapter"]
