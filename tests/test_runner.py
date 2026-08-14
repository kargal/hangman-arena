import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from arena_runner.adapters import get_adapter
from arena_runner.adapters.claude_code import ClaudeCodeAdapter
from arena_runner.config import AgentConfig, available_agents, load_agent_config
from arena_runner.run import RunOptions, agent_env, make_run_id, run_agent
from arena_runner.server_api import SessionHandle
from arena_runner.workspace import WorkspaceError, default_workspace_path, prepare_workspace
from tests.conftest import ADMIN, ROOT

FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")


def test_shipped_agent_configs_load() -> None:
    assert {"claude_code", "codex", "gemini_cli"} <= set(available_agents())
    for name in available_agents():
        cfg = load_agent_config(name)
        get_adapter(cfg.adapter)
        prompt = cfg.render_prompt(season=3, puzzles=20)
        assert "Season 3" in prompt and "20 puzzles" in prompt
        assert "4 of 20" in cfg.render_resume_prompt(remaining=4, completed=16, total=20, season=3)


def test_claude_prompt_is_short_and_neutral() -> None:
    prompt = load_agent_config("claude_code").render_prompt(season=3, puzzles=20)
    assert len(prompt.split()) < 80
    assert "`arena`" in prompt and "lower is better" in prompt


def test_claude_command_line() -> None:
    cfg = load_agent_config("claude_code")
    adapter = ClaudeCodeAdapter(cfg, model="claude-test")
    cmd = adapter.build_command("PROMPT")
    assert cmd[:3] == ["claude", "-p", "PROMPT"]
    assert "--dangerously-skip-permissions" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-test"
    assert "--resume" not in cmd
    resumed = adapter.build_command("continue", resume_session="abc-123")
    assert resumed[resumed.index("--resume") + 1] == "abc-123"


def test_claude_transcript_parsing(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    records = [
        {"type": "system", "subtype": "init", "session_id": "s-1", "model": "m", "claude_code_version": "2.3.9"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}, {"type": "text"}]}},
        {"type": "result", "subtype": "success", "session_id": "s-1", "num_turns": 7, "total_cost_usd": 0.5},
    ]
    transcript.write_text("not json\n" + "\n".join(json.dumps(r) for r in records) + "\n")
    session_id, stats = ClaudeCodeAdapter(load_agent_config("claude_code")).parse_transcript(transcript)
    assert session_id == "s-1"
    assert stats["num_turns"] == 7 and stats["tool_calls"] == 1 and stats["claude_code_version"] == "2.3.9"


def test_agent_env_passthrough_and_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")
    monkeypatch.setenv("ARENA_ADMIN_TOKEN", "secret")
    monkeypatch.setenv("UNRELATED", "x")
    env = agent_env(load_agent_config("claude_code"), "http://arena:8080", SessionHandle("ses_1", "tok", 3, 20))
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example"
    assert env["ARENA_URL"] == "http://arena:8080"
    assert env["ARENA_TOKEN"] == "tok" and env["ARENA_SESSION"] == "ses_1"
    assert "ARENA_ADMIN_TOKEN" not in env
    assert "UNRELATED" not in env


def test_run_id_and_default_workspace() -> None:
    assert make_run_id("claude_code", "claude-opus-5").endswith("-claude-code-claude-opus-5")
    assert default_workspace_path(Path("/home/agent/hangman-arena")) == Path("/home/agent/hangman-arena-agent")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def agent_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "agent-src"
    repo.mkdir()
    (repo / "README.md").write_text("# workspace\n")
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=repo)
    _git("tag", "v0.3.0", cwd=repo)
    return repo


def test_prepare_workspace_clones_fresh(tmp_path: Path, agent_repo: Path) -> None:
    dest = tmp_path / "hangman-arena-agent"
    dest.mkdir()
    (dest / "stale.txt").write_text("old")
    ws = prepare_workspace(dest, repo=str(agent_repo), ref="v0.3.0", install=False)
    assert (dest / "README.md").exists()
    assert not (dest / "stale.txt").exists()
    assert len(ws.commit) == 40


def test_prepare_workspace_bad_ref(tmp_path: Path, agent_repo: Path) -> None:
    with pytest.raises(WorkspaceError):
        prepare_workspace(tmp_path / "ws", repo=str(agent_repo), ref="v9.9.9", install=False)


def _fake_config(max_resumes: int) -> AgentConfig:
    base = load_agent_config("claude_code")
    return AgentConfig.from_dict(
        "claude_code",
        {
            "adapter": "claude_code",
            "model": "fake-model",
            "binary": str(FAKE_CLAUDE),
            "prompt": base.prompt,
            "resume_prompt": base.resume_prompt,
            "max_resumes": max_resumes,
            "env": {"FAKE_PUZZLES_PER_ATTEMPT": "7"},
        },
    )


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shebang script")
def test_run_resumes_until_session_complete(
    tmp_path: Path, agent_repo: Path, live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    run_dir = run_agent(
        _fake_config(max_resumes=5),
        RunOptions(
            server_url=live_server,
            admin_token=ADMIN,
            workspace=tmp_path / "hangman-arena-agent",
            agent_repo=str(agent_repo),
            install_workspace=False,
            runs_dir=tmp_path / "runs",
        ),
    )
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["complete"] is True
    assert meta["completed_puzzles"] == 20
    assert len(meta["attempts"]) == 3  # 7 + 7 + 6 puzzles
    assert "--resume" not in meta["attempts"][0]["command"]
    assert meta["attempts"][1]["command"][-2:] == ["--resume", "fake-session-0001"]
    assert meta["workspace"]["ref"] == "v0.3.0"
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["summary"]["puzzles"] == 20
    assert (run_dir / "prompt.txt").read_text().startswith("This is a Hangman Arena Season 3 session")
    first_transcript = Path(meta["attempts"][0]["transcript"]).read_text()
    assert "fake-session-0001" in first_transcript


def test_run_stops_at_resume_limit(tmp_path: Path, agent_repo: Path, live_server: str) -> None:
    run_dir = run_agent(
        _fake_config(max_resumes=1),
        RunOptions(
            server_url=live_server,
            admin_token=ADMIN,
            workspace=tmp_path / "hangman-arena-agent",
            agent_repo=str(agent_repo),
            install_workspace=False,
            runs_dir=tmp_path / "runs",
        ),
    )
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["complete"] is False
    assert meta["completed_puzzles"] == 14
    assert len(meta["attempts"]) == 2


def test_cli_lists_agents(capsys: pytest.CaptureFixture[str]) -> None:
    from arena_runner.cli import main

    assert main(["agents"]) == 0
    assert "claude_code" in capsys.readouterr().out


def test_arena_root_points_at_checkout() -> None:
    from arena_runner.config import ARENA_ROOT

    assert ARENA_ROOT == ROOT
