"""Adapter interface: how to launch (and resume) a particular agent CLI."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from arena_runner.config import AgentConfig

log = logging.getLogger(__name__)


@dataclass
class AttemptResult:
    attempt: int
    command: list[str]
    exit_code: int | None
    timed_out: bool
    duration_s: float
    transcript: str
    stderr_log: str
    agent_session_id: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Adapter(ABC):
    """Base class for agent adapters.

    Subclasses implement :meth:`build_command` and, if the agent can continue a
    previous conversation, set ``supports_resume`` and implement
    :meth:`parse_transcript` to extract the agent's session id.
    """

    name: ClassVar[str]
    default_binary: ClassVar[str]
    supports_resume: ClassVar[bool] = False

    def __init__(self, config: AgentConfig, model: str | None = None) -> None:
        self.config = config
        self.model = model or config.model
        self.binary = config.binary or self.default_binary

    @abstractmethod
    def build_command(self, prompt: str, resume_session: str | None = None) -> list[str]:
        """Command line for one attempt."""

    def parse_transcript(self, transcript: Path) -> tuple[str | None, dict[str, Any]]:
        """Return ``(agent_session_id, stats)`` from an attempt's stdout."""
        return None, {}

    def run_attempt(
        self,
        *,
        attempt: int,
        prompt: str,
        cwd: Path,
        env: dict[str, str],
        out_dir: Path,
        timeout_s: float,
        resume_session: str | None = None,
    ) -> AttemptResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        transcript = out_dir / f"attempt-{attempt:02d}.stdout.jsonl"
        stderr_log = out_dir / f"attempt-{attempt:02d}.stderr.log"
        command = self.build_command(prompt, resume_session)
        log.info("attempt %d: launching %s in %s", attempt, self.name, cwd)

        started = time.monotonic()
        timed_out = False
        with transcript.open("wb") as out, stderr_log.open("wb") as err:
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL)
            try:
                exit_code: int | None = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                log.warning("attempt %d: timed out after %.0fs, terminating agent", attempt, timeout_s)
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                exit_code = proc.returncode
        duration = time.monotonic() - started

        session_id, stats = self.parse_transcript(transcript)
        log.info("attempt %d: exit=%s after %.0fs (agent session %s)", attempt, exit_code, duration, session_id)
        return AttemptResult(
            attempt=attempt,
            command=_redact(command, prompt),
            exit_code=exit_code,
            timed_out=timed_out,
            duration_s=round(duration, 3),
            transcript=str(transcript),
            stderr_log=str(stderr_log),
            agent_session_id=session_id,
            stats=stats,
        )


def _redact(command: list[str], prompt: str) -> list[str]:
    """Replace the (long) prompt in a command line with a placeholder for metadata files."""
    return ["<prompt>" if part == prompt else part for part in command]


def iter_json_lines(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records
