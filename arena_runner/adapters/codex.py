"""OpenAI Codex CLI adapter (``codex exec --json``). Experimental.

Not used for leaderboard entries: every entry runs in Claude Code, with non-Anthropic models
served through an Anthropic-compatible endpoint or the arena gateway. Resume is not supported.
"""

from __future__ import annotations

from arena_runner.adapters.base import Adapter


class CodexAdapter(Adapter):
    name = "codex"
    default_binary = "codex"
    supports_resume = False

    def build_command(self, prompt: str, resume_session: str | None = None) -> list[str]:
        cmd = [self.binary, "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"]
        if self.model:
            cmd += ["--model", self.model]
        cmd += [*self.config.extra_args, prompt]
        return cmd
