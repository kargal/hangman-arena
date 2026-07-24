"""Assign difficulty tiers to puzzles in the bank.

Difficulty is estimated with a simple, reproducible baseline: a player who
guesses letters strictly in English frequency order (ETAOIN...) and never
attempts an early solve. The number of *misses* that baseline makes before the
answer is fully revealed is the puzzle's raw hardness. Short answers built from
rare letters (``jinx``, ``glyph``) score high; long answers that use common
letters score low.

Tiers are assigned per puzzle type (words and phrases are ranked separately,
since phrases expose far more letters):

* ``easy``   -- bottom 35% of raw hardness
* ``medium`` -- middle 35%
* ``hard``   -- top 30%

Usage::

    python scripts/score_difficulty.py puzzles/bank.jsonl            # report
    python scripts/score_difficulty.py puzzles/bank.jsonl --write    # rewrite tiers
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

#: English letter frequency order (most to least common).
FREQUENCY_ORDER = "ETAOINSHRDLCUMWFGYPBVKJXQZ"

TIERS = (("easy", 0.35), ("medium", 0.70), ("hard", 1.0))


def baseline_misses(answer: str) -> int:
    """Misses made by a strict frequency-order guesser on ``answer``."""
    letters = {ch for ch in answer.upper() if "A" <= ch <= "Z"}
    if not letters:
        return 0
    last = max(FREQUENCY_ORDER.index(ch) for ch in letters)
    return (last + 1) - len(letters)


def hardness(answer: str) -> float:
    """Raw hardness: baseline misses, lightly penalising very short answers."""
    letters = [ch for ch in answer.upper() if "A" <= ch <= "Z"]
    short_bonus = max(0, 6 - len(letters)) * 0.5
    return baseline_misses(answer) + short_bonus


def assign_tiers(entries: list[dict]) -> dict[str, str]:
    """Return ``{puzzle_id: tier}`` for every entry."""
    tiers: dict[str, str] = {}
    for kind in sorted({e["type"] for e in entries}):
        group = sorted(
            (e for e in entries if e["type"] == kind),
            key=lambda e: (hardness(e["answer"]), e["id"]),
        )
        n = len(group)
        for rank, entry in enumerate(group):
            q = (rank + 0.5) / n
            tiers[entry["id"]] = next(name for name, cut in TIERS if q <= cut)
    return tiers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bank", type=Path)
    parser.add_argument("--write", action="store_true", help="rewrite the bank in place")
    args = parser.parse_args(argv)

    entries = [json.loads(line) for line in args.bank.read_text().splitlines() if line.strip()]
    tiers = assign_tiers(entries)
    changed = sum(1 for e in entries if e.get("difficulty") != tiers[e["id"]])

    counts = Counter((e["type"], tiers[e["id"]]) for e in entries)
    for (kind, tier), n in sorted(counts.items()):
        print(f"{kind:7s} {tier:7s} {n:5d}")
    print(f"{changed} entr{'y' if changed == 1 else 'ies'} would change")

    if args.write and changed:
        for e in entries:
            e["difficulty"] = tiers[e["id"]]
        with args.bank.open("w") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"wrote {args.bank}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
