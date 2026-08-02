"""Orchestration of a single run: session -> workspace -> agent (-> resume) -> results."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arena_runner.adapters import AttemptResult, get_adapter
from arena_runner.config import ARENA_ROOT, AgentConfig
from arena_runner.server_api import ArenaServer, SessionHandle
from arena_runner.workspace import DEFAULT_AGENT_REPO, default_workspace_path, prepare_workspace
from arena_server.config import load_season

log = logging.getLogger(__name__)

#: Variables copied from the runner's environment into every agent process.
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TZ", "TMPDIR")


@dataclass
class RunOptions:
    server_url: str
    admin_token: str
    model: str | None = None
    season: int | None = None
    workspace: Path | None = None
    agent_repo: str | None = None
    agent_ref: str | None = None
    reuse_workspace: bool = False
    install_workspace: bool = True
    max_resumes: int | None = None
    runs_dir: Path = field(default_factory=lambda: ARENA_ROOT / "runs")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "-", text).strip("-").lower() or "default"


def make_run_id(agent: str, model: str | None, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now:%Y%m%d-%H%M%S}-{_slug(agent)}-{_slug(model or 'default')}"


def agent_env(config: AgentConfig, server_url: str, handle: SessionHandle) -> dict[str, str]:
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    env.update(config.env)
    env.update({k: os.environ[k] for k in config.env_passthrough if os.environ.get(k)})
    env.update(
        {
            "ARENA_URL": server_url,
            "ARENA_TOKEN": handle.token,
            "ARENA_SESSION": handle.session_id,
        }
    )
    return env


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def run_agent(config: AgentConfig, options: RunOptions, *, server: ArenaServer | None = None) -> Path:
    """Run ``config`` through one full session. Returns the run directory."""
    server = server or ArenaServer(options.server_url, options.admin_token)
    model = options.model or config.model
    health = server.wait_until_healthy()
    season_no = options.season or int(health["season"])
    season = load_season(season_no, home=ARENA_ROOT)

    run_id = make_run_id(config.name, model)
    run_dir = options.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    log.info("run %s -> %s", run_id, run_dir)

    workspace = prepare_workspace(
        options.workspace or default_workspace_path(),
        repo=options.agent_repo or season.agent_workspace.get("repo", DEFAULT_AGENT_REPO),
        ref=options.agent_ref if options.agent_ref is not None else season.agent_workspace.get("ref"),
        reuse=options.reuse_workspace,
        install=options.install_workspace,
    )

    handle = server.create_session(agent=config.name, model=model, run_id=run_id, season=season_no)
    log.info("session %s: season %d, %d puzzles", handle.session_id, handle.season, handle.total)

    adapter = get_adapter(config.adapter)(config, model)
    env = agent_env(config, options.server_url, handle)
    prompt = config.render_prompt(season=handle.season, puzzles=handle.total)
    (run_dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")

    meta: dict[str, Any] = {
        "run_id": run_id,
        "agent": config.name,
        "adapter": config.adapter,
        "model": model,
        "season": handle.season,
        "session_id": handle.session_id,
        "server_url": options.server_url,
        "workspace": {
            "path": str(workspace.path),
            "repo": workspace.repo,
            "ref": workspace.ref,
            "commit": workspace.commit,
        },
        "started_at": datetime.now(timezone.utc).isoformat(),
        "attempts": [],
    }
    _write_json(run_dir / "meta.json", meta)

    max_resumes = config.max_resumes if options.max_resumes is None else options.max_resumes
    attempts: list[AttemptResult] = []
    resume_session: str | None = None
    session: dict[str, Any] = {}

    for attempt in range(1, max_resumes + 2):
        result = adapter.run_attempt(
            attempt=attempt,
            prompt=prompt,
            cwd=workspace.path,
            env=env,
            out_dir=run_dir / "attempts",
            timeout_s=config.attempt_timeout_minutes * 60,
            resume_session=resume_session,
        )
        attempts.append(result)
        meta["attempts"] = [a.to_dict() for a in attempts]
        _write_json(run_dir / "meta.json", meta)

        session = server.status(handle)["session"]
        if session["finished"]:
            log.info("session complete after %d attempt(s)", attempt)
            break
        remaining = session["total"] - session["completed"]
        if attempt > max_resumes:
            log.warning("resume limit (%d) reached with %d puzzle(s) remaining", max_resumes, remaining)
            break
        if not adapter.supports_resume or not result.agent_session_id:
            log.warning("agent stopped with %d puzzle(s) remaining and cannot be resumed", remaining)
            break
        resume_session = result.agent_session_id
        prompt = config.render_resume_prompt(
            remaining=remaining, completed=session["completed"], total=session["total"], season=handle.season
        )
        log.info("agent exited with %d puzzle(s) remaining; resuming %s", remaining, resume_session)

    summary = server.summary(handle)
    _write_json(run_dir / "summary.json", summary)
    _write_json(run_dir / "history.json", server.history(handle))
    meta.update(
        finished_at=datetime.now(timezone.utc).isoformat(),
        complete=bool(session.get("finished")),
        completed_puzzles=session.get("completed"),
        average_guesses=summary["summary"]["average_guesses"],
        learning_curve_slope=summary["summary"]["learning_curve_slope"],
    )
    _write_json(run_dir / "meta.json", meta)
    return run_dir
