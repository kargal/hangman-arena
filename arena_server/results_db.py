"""Results database: one row per session and one row per finished puzzle.

The server writes every session and every finished puzzle to a SQL database and
computes the season's live standings from it. Configure it with ``ARENA_DB_URL``
(an SQLAlchemy URL); the default is an SQLite file next to the event logs::

    ARENA_DB_URL=sqlite:////srv/arena/results/arena.db                  # default
    ARENA_DB_URL=postgresql://arena:secret@db:5432/arena                 # Postgres (psycopg 3)

Schema (created on start-up if missing)::

    sessions        session_id PK, season, label, agent, model, run_id, total, created_at, finished_at
    puzzle_results  (session_id, puzzle_index) PK, season, puzzle_id, status, score, finished_at

The JSONL event log (:mod:`arena_server.events`) stays the source of truth for
replaying sessions; on start-up the restored sessions are written to the
database again (existing rows are kept), so a fresh database can be rebuilt
from the logs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    select,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.dml import Insert

log = logging.getLogger(__name__)

#: Display names of the runner's agent adapters, used for default entry labels.
AGENT_NAMES = {"claude_code": "Claude Code", "codex": "Codex CLI", "gemini_cli": "Gemini CLI"}

#: Number of entries returned in the standings (the requesting session is always included).
STANDINGS_LIMIT = 25

metadata = MetaData()

sessions_table = Table(
    "sessions",
    metadata,
    Column("session_id", String(64), primary_key=True),
    Column("season", Integer, nullable=False, index=True),
    Column("label", String(200), nullable=False),
    Column("agent", String(64), nullable=False),
    Column("model", String(128)),
    Column("run_id", String(128)),
    Column("total", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
)

puzzle_results_table = Table(
    "puzzle_results",
    metadata,
    Column("session_id", String(64), ForeignKey("sessions.session_id"), primary_key=True),
    Column("puzzle_index", Integer, primary_key=True),
    Column("season", Integer, nullable=False, index=True),
    Column("puzzle_id", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("score", Integer, nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=False),
)


def default_label(agent: str, model: str | None) -> str:
    """``"<model> (<agent display name>)"``, e.g. ``"claude-opus-5 (Claude Code)"``."""
    return f"{model or 'unknown model'} ({AGENT_NAMES.get(agent, agent)})"


def standings_notice(rank: int, entries: int, average: float, leader_average: float, remaining: int) -> str:
    """One-line summary of a session's place in the standings, shown to the agent with the table.

    ``rank`` and ``average`` are the session's own, ``entries`` is the number of ranked entries in the season,
    ``leader_average`` the average of the first entry and ``remaining`` the puzzles the session still has to play.
    """
    place = f"You are #{rank} of {entries}"
    if rank == 1:
        return f"{place}."
    gap = max(average - leader_average, 0.0)  # exact averages, rounded once for display
    if f"{gap:.1f}" != "0.0":
        behind = f"the leader averages {gap:.1f} fewer guesses per puzzle."
    elif f"{gap:.2f}" != "0.00":
        behind = f"the leader averages {gap:.2f} fewer guesses per puzzle."
    else:  # same average, more puzzles finished
        behind = "the leader has the same average over more finished puzzles."
    notice = f"{place} - {behind}"
    if remaining > 0:
        notice += " You may need some extra work in the next puzzles to catch up."
    return notice


def normalize_url(url: str) -> URL:
    """Parse ``url``; plain ``postgresql://`` / ``postgres://`` URLs use the psycopg 3 driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    parsed = make_url(url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed


def parse_ts(value: str | datetime | None) -> datetime:
    """Event-log timestamps (``2026-10-21T09:30:12.123456Z``) as aware datetimes."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    if value.tzinfo is None:  # SQLite returns naive datetimes
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PuzzleRow:
    index: int
    puzzle_id: str
    status: str
    score: int
    finished_at: datetime


class ResultsDB:
    """Thin SQLAlchemy Core wrapper; safe to share between request threads."""

    def __init__(self, url: str, *, engine: Engine | None = None) -> None:
        self.url = normalize_url(url)
        if engine is None:
            kwargs: dict[str, Any] = {"pool_pre_ping": True}
            if self.url.get_backend_name() == "sqlite":
                kwargs["connect_args"] = {"check_same_thread": False}
                if self.url.database and self.url.database != ":memory:":
                    Path(self.url.database).parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(self.url, **kwargs)
        self.engine = engine
        self.dialect = engine.dialect.name

    @property
    def safe_url(self) -> str:
        """The URL with the password masked (for logs)."""
        return self.url.render_as_string(hide_password=True)

    def create_schema(self) -> None:
        metadata.create_all(self.engine, checkfirst=True)

    def dispose(self) -> None:
        self.engine.dispose()

    # -- writes ------------------------------------------------------------

    def _insert_ignore(self, table: Table) -> Insert:
        if self.dialect == "postgresql":
            return postgresql.insert(table).on_conflict_do_nothing()
        if self.dialect == "sqlite":
            return sqlite.insert(table).on_conflict_do_nothing()
        raise RuntimeError(f"unsupported database dialect: {self.dialect}")  # pragma: no cover

    def record_session(
        self,
        *,
        session_id: str,
        season: int,
        label: str,
        agent: str,
        model: str | None,
        run_id: str | None,
        total: int,
        created_at: str | datetime,
        conn: Connection | None = None,
    ) -> None:
        row = {
            "session_id": session_id,
            "season": season,
            "label": label,
            "agent": agent,
            "model": model,
            "run_id": run_id,
            "total": total,
            "created_at": parse_ts(created_at),
        }
        if conn is not None:
            conn.execute(self._insert_ignore(sessions_table), [row])
            return
        with self.engine.begin() as c:
            c.execute(self._insert_ignore(sessions_table), [row])

    def record_puzzle(
        self,
        *,
        session_id: str,
        season: int,
        puzzle: PuzzleRow,
        session_finished: bool,
        conn: Connection | None = None,
    ) -> None:
        """Record one finished puzzle (idempotent) and, after the last one, the session's end time."""

        def _write(c: Connection) -> None:
            c.execute(
                self._insert_ignore(puzzle_results_table),
                [
                    {
                        "session_id": session_id,
                        "puzzle_index": puzzle.index,
                        "season": season,
                        "puzzle_id": puzzle.puzzle_id,
                        "status": puzzle.status,
                        "score": puzzle.score,
                        "finished_at": puzzle.finished_at,
                    }
                ],
            )
            if session_finished:
                c.execute(
                    update(sessions_table)
                    .where(sessions_table.c.session_id == session_id, sessions_table.c.finished_at.is_(None))
                    .values(finished_at=puzzle.finished_at)
                )

        if conn is not None:
            _write(conn)
            return
        with self.engine.begin() as c:
            _write(c)

    # -- reads -------------------------------------------------------------

    def scores(self, session_id: str) -> list[int]:
        """Recorded scores of one session, in puzzle order."""
        query = (
            select(puzzle_results_table.c.score)
            .where(puzzle_results_table.c.session_id == session_id)
            .order_by(puzzle_results_table.c.puzzle_index)
        )
        with self.engine.connect() as c:
            return [int(s) for s in c.execute(query).scalars()]

    def standings(self, season: int, *, session_id: str | None = None, limit: int = STANDINGS_LIMIT) -> dict[str, Any]:
        """Live standings of ``season``: every session with at least one finished puzzle.

        Ranked by average guesses (lower is better), then by more finished puzzles; entries with the same
        average and the same number of finished puzzles share a rank. ``session_id`` is marked ``you`` and
        always included, even below ``limit``, and gets a one-line ``notice`` on its place (see
        :func:`standings_notice`; ``None`` while it has no finished puzzle).
        """
        s, r = sessions_table, puzzle_results_table
        completed = func.count(r.c.puzzle_index).label("completed")
        average = func.avg(r.c.score).label("average_guesses")
        query = (
            select(s.c.session_id, s.c.label, s.c.model, s.c.agent, s.c.total, s.c.created_at, completed, average)
            .join(r, r.c.session_id == s.c.session_id)
            .where(s.c.season == season)
            .group_by(s.c.session_id, s.c.label, s.c.model, s.c.agent, s.c.total, s.c.created_at)
        )
        with self.engine.connect() as c:
            rows = c.execute(query).all()
        ordered = sorted(rows, key=lambda row: (round(float(row.average_guesses), 6), -row.completed, row.created_at))
        entries: list[dict[str, Any]] = []
        notice: str | None = None
        previous: tuple[float, int] | None = None
        rank = 0
        for position, row in enumerate(ordered, start=1):
            avg = round(float(row.average_guesses), 3)
            key = (avg, int(row.completed))
            if key != previous:
                rank = position
            previous = key
            if row.session_id == session_id:
                leader = float(ordered[0].average_guesses)
                notice = standings_notice(
                    rank, len(ordered), float(row.average_guesses), leader, int(row.total) - int(row.completed)
                )
            if position > limit and row.session_id != session_id:
                continue
            entries.append(
                {
                    "rank": rank,
                    "label": row.label,
                    "model": row.model,
                    "agent": row.agent,
                    "completed": int(row.completed),
                    "average_guesses": avg,
                    "you": row.session_id == session_id,
                }
            )
        return {"season": season, "as_of": _iso(datetime.now(timezone.utc)), "notice": notice, "entries": entries}


__all__ = [
    "AGENT_NAMES",
    "STANDINGS_LIMIT",
    "PuzzleRow",
    "ResultsDB",
    "SQLAlchemyError",
    "default_label",
    "normalize_url",
    "parse_ts",
    "standings_notice",
]
