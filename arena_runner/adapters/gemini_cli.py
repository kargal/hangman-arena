"""Gemini CLI adapter (``gemini -p ... --yolo``). Experimental.

Not used for leaderboard entries: every entry runs in Claude Code, with non-Anthropic models
served through an Anthropic-compatible endpoint or the arena gateway. Resume is not supported.
"""

from __future__ import annotations

from arena_runner.adapters.base import Adapter


class GeminiCliAdapter(Adapter):
    name = "gemini_cli"
    default_binary = "gemini"
    supports_resume = False

    def build_command(self, prompt: str, resume_session: str | None = None) -> list[str]:
        cmd = [self.binary, "--yolo", "--output-format", "stream-json"]
        if self.model:
            cmd += ["--model", self.model]
        cmd += [*self.config.extra_args, "-p", prompt]
        return cmd
