"""FastAPI application implementing the Hangman Arena protocol (v1).

The client-facing protocol is documented in the agent workspace
(``hangman-arena-agent/docs/protocol.md``); administrative endpoints are
documented in ``docs/maintainers.md``.
"""

# NOTE: no ``from __future__ import annotations`` here -- FastAPI resolves the
# dependency annotations of the nested route functions at definition time.
import hmac
import logging
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from hangman_engine import HangmanError
from pydantic import BaseModel, Field

from arena_server import __version__
from arena_server.config import Settings, load_season
from arena_server.errors import ArenaHTTPError, conflict, unauthorized
from arena_server.events import EventLog
from arena_server.schedule import season_schedule
from arena_server.sessions import Session, SessionStore

log = logging.getLogger(__name__)


class CreateSessionRequest(BaseModel):
    agent: str = Field("unknown", max_length=64)
    model: str | None = Field(None, max_length=128)
    run_id: str | None = Field(None, max_length=128)
    season: int | None = None


class GuessRequest(BaseModel):
    letter: str = Field(..., max_length=8)


class SolveRequest(BaseModel):
    answer: str = Field(..., max_length=256)


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    season = load_season(settings.season, home=settings.home)
    schedule = season_schedule(settings.season, home=settings.home)
    store = SessionStore(EventLog(settings.results_dir))
    restored = store.replay({season.season: schedule})
    log.info(
        "serving %s: %d puzzles per session, %d session(s) restored from %s",
        season.name,
        len(schedule),
        restored,
        settings.results_dir,
    )

    app = FastAPI(title="Hangman Arena", version=__version__, docs_url="/docs", redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.season = season

    # -- errors -------------------------------------------------------------

    @app.exception_handler(ArenaHTTPError)
    async def _arena_error(_: Request, exc: ArenaHTTPError) -> JSONResponse:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        message = f"{where}: {first.get('msg', 'invalid request')}" if where else "invalid request"
        return JSONResponse({"error": {"code": "bad_request", "message": message}}, status_code=422)

    # -- dependencies -------------------------------------------------------

    def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
        token = _bearer(authorization)
        if not token or not hmac.compare_digest(token, settings.admin_token):
            raise unauthorized("admin token required")

    def session_auth(session_id: str, authorization: Annotated[str | None, Header()] = None) -> Session:
        return store.authenticate(session_id, _bearer(authorization))

    SessionDep = Annotated[Session, Depends(session_auth)]

    # -- public -------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "season": season.season, "version": __version__}

    @app.get("/v1/season")
    def current_season() -> dict[str, Any]:
        return {
            "season": season.season,
            "name": season.name,
            "puzzles_per_session": season.puzzles_per_session,
            "max_guesses": season.max_guesses,
            "opens": season.opens,
        }

    # -- admin --------------------------------------------------------------

    @app.post("/v1/sessions", status_code=201, dependencies=[Depends(require_admin)])
    def create_session(body: CreateSessionRequest) -> dict[str, Any]:
        if body.season is not None and body.season != season.season:
            raise conflict("wrong_season", f"this server is running season {season.season}, not {body.season}")
        session, token = store.create(
            season=season.season,
            schedule=schedule,
            max_guesses=season.max_guesses,
            agent=body.agent,
            model=body.model,
            run_id=body.run_id,
        )
        log.info("created %s for agent=%s model=%s", session.session_id, body.agent, body.model)
        return {
            "session_id": session.session_id,
            "token": token,
            "season": season.season,
            "total": len(schedule),
            "max_guesses": season.max_guesses,
        }

    @app.get("/v1/sessions", dependencies=[Depends(require_admin)])
    def list_sessions() -> dict[str, Any]:
        return {"sessions": [s.progress() for s in store.all()]}

    # -- session ------------------------------------------------------------

    @app.get("/v1/sessions/{session_id}")
    def get_session(session: SessionDep) -> dict[str, Any]:
        with session.lock:
            return {"session": session.progress(), "puzzle": session.current_view()}

    @app.post("/v1/sessions/{session_id}/next")
    def next_puzzle(session: SessionDep) -> dict[str, Any]:
        with session.lock:
            state = session.start_next()
            store.log(session, "puzzle_started", index=len(session.states), puzzle_id=state.puzzle.puzzle_id)
            return {"session": session.progress(), "puzzle": session.current_view()}

    def _move(session: Session, kind: str, value: str) -> dict[str, Any]:
        with session.lock:
            state = session.active()
            try:
                result = state.guess_letter(value) if kind == "guess" else state.solve(value)
            except HangmanError as exc:
                store.log(session, "rejected", kind=kind, value=value, code=exc.code)
                raise ArenaHTTPError(400 if exc.code == "invalid_guess" else 409, exc.code, str(exc)) from exc
            field = "letter" if kind == "guess" else "answer"
            store.log(
                session,
                kind,
                index=len(session.states),
                puzzle_id=state.puzzle.puzzle_id,
                **{field: value},
                correct=result.correct,
                guesses=result.guesses,
            )
            if state.finished:
                store.log_puzzle_end(session, state)
            body: dict[str, Any] = {
                "session": session.progress(),
                "puzzle": session.current_view(),
                "result": result.to_dict(),
            }
            if session.finished:
                body["summary"] = session.summary()["summary"]
            return body

    @app.post("/v1/sessions/{session_id}/guess")
    def guess(body: GuessRequest, session: SessionDep) -> dict[str, Any]:
        return _move(session, "guess", body.letter)

    @app.post("/v1/sessions/{session_id}/solve")
    def solve(body: SolveRequest, session: SessionDep) -> dict[str, Any]:
        return _move(session, "solve", body.answer)

    @app.get("/v1/sessions/{session_id}/history")
    def history(session: SessionDep) -> dict[str, Any]:
        with session.lock:
            done = [session.puzzle_view(i) for i, s in enumerate(session.states, start=1) if s.finished]
            return {"session": session.progress(), "puzzles": done}

    @app.get("/v1/sessions/{session_id}/summary")
    def summary(session: SessionDep) -> dict[str, Any]:
        with session.lock:
            return session.summary()

    return app
