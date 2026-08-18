"""Render leaderboard/*.json into README.md between the leaderboard markers.

python scripts/render_leaderboard.py           # rewrite README.md
python scripts/render_leaderboard.py --check   # exit 1 if README.md is stale (used in CI)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
START = "<!-- leaderboard:start -->"
END = "<!-- leaderboard:end -->"


def load_seasons(directory: Path) -> list[dict[str, Any]]:
    seasons = [json.loads(p.read_text(encoding="utf-8")) for p in directory.glob("season-*.json")]
    return sorted(seasons, key=lambda s: s["season"], reverse=True)


def render_table(season: dict[str, Any]) -> str:
    entries = sorted(season["entries"], key=lambda e: (e["avg_guesses"], e["model"]))
    lines = [
        "| # | Model | Endpoint | Avg. guesses / puzzle ↓ | Learning slope ↓ | Solve rate | Sessions |",
        "|---:|---|---|---:|---:|---:|---:|",
    ]
    for rank, e in enumerate(entries, start=1):
        lines.append(
            f"| {rank} | {e['model']} | {e['endpoint']} | **{e['avg_guesses']:.2f}** ± {e['ci95']:.2f} "
            f"| {e['learning_slope']:+.3f} | {e['solve_rate'] * 100:.0f}% | {e['sessions']} |"
        )
    return "\n".join(lines)


def render(seasons: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for season in seasons:
        title = f"### Season {season['season']}"
        if season["status"] == "in_progress":
            title += f" (in progress, opened {season['opens']})"
        else:
            title += f" (final, {season['opens']} to {season['closes']})"
        blocks.append(title)
        if season.get("harness"):
            blocks.append(f"Harness: **{season['harness']}** for every entry.")
        if season["entries"]:
            blocks.append(render_table(season))
            blocks.append(f"<sub>{season['notes']} Last updated {season['updated']}.</sub>")
        else:
            blocks.append(f"_{season['notes']}_")
    return "\n\n".join(blocks)


def splice(readme: str, body: str) -> str:
    try:
        head, rest = readme.split(START, 1)
        _, tail = rest.split(END, 1)
    except ValueError:
        raise SystemExit(f"README.md must contain {START} and {END}") from None
    return f"{head}{START}\n{body}\n{END}{tail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if README.md is not up to date")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    parser.add_argument("--dir", type=Path, default=ROOT / "leaderboard")
    args = parser.parse_args(argv)

    current = args.readme.read_text(encoding="utf-8")
    updated = splice(current, render(load_seasons(args.dir)))
    if args.check:
        if updated != current:
            print("README.md leaderboard is stale; run: python scripts/render_leaderboard.py", file=sys.stderr)
            return 1
        print("README.md leaderboard is up to date")
        return 0
    if updated != current:
        args.readme.write_text(updated, encoding="utf-8")
        print(f"updated {args.readme}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
