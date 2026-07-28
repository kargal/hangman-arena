"""Append-only JSONL event log, one file per session.

Layout::

    results/
      season-3/
        ses_4f1c2a9b0e7d3c11.jsonl          # every event, in order
        ses_4f1c2a9b0e7d3c11.summary.json   # written when the session finishes

Event types: ``session_created``, ``puzzle_started``, ``guess``, ``solve``,
``rejected``, ``puzzle_finished``, ``session_finished``. The log is the source
of truth for scoring and is replayed on server start-up.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class EventLog:
    def __init__(self, results_dir: Path) -> None:
        self.results_dir = results_dir
        self._lock = threading.Lock()

    def season_dir(self, season: int) -> Path:
        path = self.results_dir / f"season-{season}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path(self, season: int, session_id: str) -> Path:
        return self.season_dir(season) / f"{session_id}.jsonl"

    def append(self, season: int, session_id: str, event: str, **data: Any) -> dict[str, Any]:
        record = {"ts": utcnow(), "event": event, "season": season, "session_id": session_id, **data}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, self.path(season, session_id).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return record

    def write_summary(self, season: int, session_id: str, summary: dict[str, Any]) -> Path:
        path = self.season_dir(season) / f"{session_id}.summary.json"
        path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def iter_logs(self) -> Iterator[list[dict[str, Any]]]:
        """Yield the events of every session log found under ``results_dir``."""
        if not self.results_dir.exists():
            return
        for path in sorted(self.results_dir.glob("season-*/ses_*.jsonl")):
            with path.open(encoding="utf-8") as fh:
                yield [json.loads(line) for line in fh if line.strip()]
