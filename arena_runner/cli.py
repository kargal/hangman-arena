"""``arena-run`` / ``python -m arena_runner`` -- run an agent through a session.

python -m arena_runner run --agent claude_code --model claude-opus-5
python -m arena_runner agents
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from arena_runner.adapters import ADAPTERS
from arena_runner.config import ARENA_ROOT, AgentConfigError, available_agents, load_agent_config
from arena_runner.run import RunOptions, run_agent
from arena_runner.server_api import ServerError
from arena_runner.workspace import WorkspaceError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arena-run", description="Run an LLM agent through a Hangman Arena session.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one session")
    run.add_argument("--agent", required=True, help="agent config name (configs/agents/<name>.yaml)")
    run.add_argument("--model", help="model id passed to the agent (overrides the config)")
    run.add_argument("--server", default=os.environ.get("ARENA_URL", "http://arena:8080"), help="arena server URL")
    run.add_argument(
        "--admin-token", default=os.environ.get("ARENA_ADMIN_TOKEN"), help="server admin token ($ARENA_ADMIN_TOKEN)"
    )
    run.add_argument("--season", type=int, help="season to run (default: the server's season)")
    run.add_argument("--workspace", type=Path, help="agent workspace path (default: ../hangman-arena-agent)")
    run.add_argument("--agent-repo", help="git URL or path of hangman-arena-agent (default: from season config)")
    run.add_argument("--agent-ref", help="branch or tag to check out (default: from season config)")
    run.add_argument("--reuse-workspace", action="store_true", help="do not re-clone an existing workspace")
    run.add_argument("--no-install", action="store_true", help="skip `pip install -e` of the workspace")
    run.add_argument("--max-resumes", type=int, help="override max_resumes from the agent config")
    run.add_argument("--runs-dir", type=Path, default=ARENA_ROOT / "runs")

    sub.add_parser("agents", help="list agent configs and adapters")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.command == "agents":
        for name in available_agents():
            cfg = load_agent_config(name)
            print(f"{name:12s} adapter={cfg.adapter:12s} model={cfg.model or '-'}")
        print(f"\nadapters: {', '.join(sorted(ADAPTERS))}")
        return 0

    if not args.admin_token:
        print("arena-run: --admin-token or ARENA_ADMIN_TOKEN is required", file=sys.stderr)
        return 2
    try:
        config = load_agent_config(args.agent)
        run_dir = run_agent(
            config,
            RunOptions(
                server_url=args.server,
                admin_token=args.admin_token,
                model=args.model,
                season=args.season,
                workspace=args.workspace,
                agent_repo=args.agent_repo,
                agent_ref=args.agent_ref,
                reuse_workspace=args.reuse_workspace,
                install_workspace=not args.no_install,
                max_resumes=args.max_resumes,
                runs_dir=args.runs_dir,
            ),
        )
    except (AgentConfigError, ServerError, WorkspaceError, KeyError) as exc:
        print(f"arena-run: {exc}", file=sys.stderr)
        return 1
    print(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
