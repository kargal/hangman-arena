"""Claude Code adapter (``claude -p ... --output-format stream-json``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arena_runner.adapters.base import Adapter, iter_json_lines


class ClaudeCodeAdapter(Adapter):
    """Runs Claude Code headless with full tool permissions inside the workspace.

    Non-Anthropic models served behind an Anthropic-compatible endpoint can be
    run by passing ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN`` through to
    the agent (see ``env_passthrough`` in ``configs/agents/claude_code.yaml``).
    """

    name = "claude_code"
    default_binary = "claude"
    supports_resume = True

    def build_command(self, prompt: str, resume_session: str | None = None) -> list[str]:
        cmd = [
            self.binary,
            "-p",
            prompt,
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if resume_session:
            cmd += ["--resume", resume_session]
        cmd += list(self.config.extra_args)
        return cmd

    def parse_transcript(self, transcript: Path) -> tuple[str | None, dict[str, Any]]:
        session_id: str | None = None
        stats: dict[str, Any] = {}
        tool_calls = 0
        for record in iter_json_lines(transcript):
            if session_id is None and isinstance(record.get("session_id"), str):
                session_id = record["session_id"]
            if record.get("type") == "system" and record.get("subtype") == "init":
                stats["model"] = record.get("model")
                stats["claude_code_version"] = record.get("claude_code_version")
            elif record.get("type") == "assistant":
                content = (record.get("message") or {}).get("content") or []
                tool_calls += sum(1 for block in content if isinstance(block, dict) and block.get("type") == "tool_use")
            elif record.get("type") == "result":
                session_id = record.get("session_id", session_id)
                for key in ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd", "usage"):
                    if key in record:
                        stats[key] = record[key]
        stats["tool_calls"] = tool_calls
        return session_id, stats
