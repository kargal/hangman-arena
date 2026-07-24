"""Validate puzzles/bank.jsonl: schema, ids, duplicates, characters, difficulty tiers.

    python scripts/validate_bank.py [puzzles/bank.jsonl]

Checks performed:

* every line parses and has ``id``, ``answer``, ``type``, ``category``, ``difficulty``;
* ids are unique four-digit strings; answers are unique (case/whitespace-insensitive);
* answers use only ASCII letters, single spaces, hyphens, apostrophes and commas;
* ``type`` matches the answer (words have no spaces);
* difficulty tiers agree with ``scripts/score_difficulty.py``;
* every season config can be scheduled from the bank.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from score_difficulty import assign_tiers  # noqa: E402

from arena_server.bank import BankError, load_bank, validate_entries  # noqa: E402
from arena_server.schedule import ScheduleError, season_schedule  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = Path(argv[0]) if argv else ROOT / "puzzles" / "bank.jsonl"
    try:
        entries = load_bank(path, validate=False)
    except BankError as exc:
        print(f"FAIL {exc}")
        return 1

    problems = validate_entries(entries)
    tiers = assign_tiers([{"id": e.id, "answer": e.answer, "type": e.type} for e in entries])
    problems += [
        f"entry {e.id!r}: difficulty {e.difficulty!r} but score_difficulty.py says {tiers[e.id]!r}"
        for e in entries
        if tiers[e.id] != e.difficulty
    ]

    for config in sorted((ROOT / "configs").glob("season-*.yaml")):
        season = int(config.stem.split("-")[1])
        try:
            season_schedule(season, home=ROOT)
        except (ScheduleError, BankError) as exc:
            problems.append(f"{config.name}: {exc}")

    by_type = Counter(e.type for e in entries)
    print(
        f"{len(entries)} puzzles ({by_type['word']} words, {by_type['phrase']} phrases), "
        f"{len({e.category for e in entries})} categories"
    )
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
