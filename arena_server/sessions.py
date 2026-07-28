"""Session state and the in-memory session store."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

from hangman_engine import HangmanError, PuzzleState, summarize

from arena_server.errors import conflict, not_found, unauthorized
from arena_server.events import EventLog, utcnow
from arena_server.schedule import ScheduledPuzzle

log = logging.getLogger(__name__)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Session:
    session_id: str
    token_hash: str
    season: int
    schedule: list[ScheduledPuzzle]
    max_guesses: int
    agent: str
    model: str | None
    run_id: str | None
    created_at: str
    states: list[PuzzleState] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- views -----------------------------------------------------------

    @property
    def current(self) -> PuzzleState | None:
        return self.states[-1] if self.states else None

    @property
    def completed(self) -> int:
        return sum(1 for s in self.states if s.finished)

    @property
    def finished(self) -> bool:
        return self.completed == len(self.schedule)

    def progress(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "season": self.season,
            "total": len(self.schedule),
            "completed": self.completed,
            "current_index": len(self.states),
            "finished": self.finished,
            "average_guesses": summarize(self.states).average_guesses,
            "agent": self.agent,
            "model": self.model,
            "created_at": self.created_at,
        }

    def puzzle_view(self, index: int) -> dict[str, Any]:
        return {"index": index, "total": len(self.schedule), **self.states[index - 1].view()}

    def current_view(self) -> dict[str, Any] | None:
        return self.puzzle_view(len(self.states)) if self.states else None

    def summary(self) -> dict[str, Any]:
        return {
            "session": self.progress(),
            "scores": [s.score for s in self.states if s.finished],
            "summary": summarize(self.states).to_dict(),
        }

    # -- actions (callers must hold ``lock``) ----------------------------

    def start_next(self) -> PuzzleState:
        if self.current is not None and not self.current.finished:
            raise conflict("puzzle_in_progress", "finish the current puzzle before requesting the next one")
        if len(self.states) >= len(self.schedule):
            raise conflict("session_complete", f"all {len(self.schedule)} puzzles in this session are finished")
        scheduled = self.schedule[len(self.states)]
        state = PuzzleState(scheduled.entry.to_puzzle(self.season), max_guesses=self.max_guesses)
        self.states.append(state)
        return state

    def active(self) -> PuzzleState:
        state = self.current
        if state is None or state.finished:
            raise conflict("no_active_puzzle", "no puzzle in progress; run `arena next` to start one")
        return state


class SessionStore:
    """Thread-safe registry of sessions, backed by the event log."""

    def __init__(self, events: EventLog) -> None:
        self.events = events
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._sessions)

    def all(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    def create(
        self,
        *,
        season: int,
        schedule: list[ScheduledPuzzle],
        max_guesses: int,
        agent: str,
        model: str | None,
        run_id: str | None,
    ) -> tuple[Session, str]:
        token = secrets.token_urlsafe(24)
        session = Session(
            session_id=f"ses_{secrets.token_hex(8)}",
            token_hash=hash_token(token),
            season=season,
            schedule=schedule,
            max_guesses=max_guesses,
            agent=agent,
            model=model,
            run_id=run_id,
            created_at=utcnow(),
        )
        with self._lock:
            self._sessions[session.session_id] = session
        self.events.append(
            season,
            session.session_id,
            "session_created",
            created_at=session.created_at,
            token_sha256=session.token_hash,
            agent=agent,
            model=model,
            run_id=run_id,
            total=len(schedule),
            max_guesses=max_guesses,
            schedule=[p.puzzle_id for p in schedule],
        )
        return session, token

    def get(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise not_found(session_id)
        return session

    def authenticate(self, session_id: str, token: str | None) -> Session:
        session = self.get(session_id)
        if not token or not hmac.compare_digest(hash_token(token), session.token_hash):
            raise unauthorized("invalid token for this session")
        return session

    # -- logging helpers ---------------------------------------------------

    def log(self, session: Session, event: str, **data: Any) -> None:
        self.events.append(session.season, session.session_id, event, **data)

    def log_puzzle_end(self, session: Session, state: PuzzleState) -> None:
        self.log(
            session,
            "puzzle_finished",
            index=len(session.states),
            puzzle_id=state.puzzle.puzzle_id,
            status=state.status.value,
            score=state.score,
            answer=state.answer,
        )
        if session.finished:
            summary = session.summary()
            self.log(session, "session_finished", **summary["summary"])
            self.events.write_summary(session.season, session.session_id, summary)

    # -- replay ------------------------------------------------------------

    def replay(self, schedules: dict[int, list[ScheduledPuzzle]]) -> int:
        """Rebuild sessions from the event log. Returns the number restored."""
        restored = 0
        for events in self.events.iter_logs():
            if not events or events[0].get("event") != "session_created":
                continue
            head = events[0]
            season = int(head["season"])
            if season not in schedules:
                log.warning("skipping session %s: season %s is not loaded", head["session_id"], season)
                continue
            schedule = schedules[season]
            if [p.puzzle_id for p in schedule] != head["schedule"]:
                log.warning("skipping session %s: schedule changed since it was created", head["session_id"])
                continue
            session = Session(
                session_id=head["session_id"],
                token_hash=head["token_sha256"],
                season=season,
                schedule=schedule,
                max_guesses=int(head.get("max_guesses", 30)),
                agent=head.get("agent", "unknown"),
                model=head.get("model"),
                run_id=head.get("run_id"),
                created_at=head.get("created_at", head["ts"]),
            )
            try:
                for ev in events[1:]:
                    if ev["event"] == "puzzle_started":
                        session.start_next()
                    elif ev["event"] == "guess":
                        session.active().guess_letter(ev["letter"])
                    elif ev["event"] == "solve":
                        session.active().solve(ev["answer"])
            except (HangmanError, KeyError, ValueError) as exc:  # pragma: no cover - corrupted log
                log.error("could not replay session %s: %s", session.session_id, exc)
                continue
            with self._lock:
                self._sessions[session.session_id] = session
            restored += 1
        return restored
