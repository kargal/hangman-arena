"""Preparing the agent workspace (a fresh clone of ``hangman-arena-agent``).

By default the workspace is cloned *next to* the arena checkout::

    ~/hangman-arena/          <- this repository (runner, server, configs)
    ~/hangman-arena-agent/    <- agent workspace; the agent is launched here
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from arena_runner.config import ARENA_ROOT

log = logging.getLogger(__name__)

DEFAULT_AGENT_REPO = "https://github.com/kargal/hangman-arena-agent.git"


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Workspace:
    path: Path
    repo: str
    ref: str | None
    commit: str


def default_workspace_path(arena_root: Path = ARENA_ROOT) -> Path:
    return arena_root.parent / "hangman-arena-agent"


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def prepare_workspace(
    dest: Path,
    *,
    repo: str = DEFAULT_AGENT_REPO,
    ref: str | None = None,
    reuse: bool = False,
    install: bool = True,
) -> Workspace:
    """Clone ``repo`` at ``ref`` into ``dest`` and install it into this Python environment.

    An existing ``dest`` is removed first unless ``reuse`` is set, so every run starts
    from a pristine workspace.
    """
    dest = dest.resolve()
    if dest.exists() and not reuse:
        log.info("removing previous workspace %s", dest)
        shutil.rmtree(dest)
    if not dest.exists():
        args = ["clone", "--quiet", "--depth", "1"]
        if ref:
            args += ["--branch", ref]
        log.info("cloning %s%s into %s", repo, f"@{ref}" if ref else "", dest)
        _git(*args, repo, str(dest))
    commit = _git("rev-parse", "HEAD", cwd=dest)

    if install:
        log.info("installing agent workspace (arena CLI)")
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-e", str(dest)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise WorkspaceError(f"pip install failed:\n{proc.stderr}")
        if shutil.which("arena", path=str(Path(sys.executable).parent)) is None and shutil.which("arena") is None:
            raise WorkspaceError("`arena` CLI not found on PATH after installing the workspace")
    return Workspace(path=dest, repo=repo, ref=ref, commit=commit)
