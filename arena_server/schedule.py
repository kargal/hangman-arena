"""Deterministic season schedules.

Every session in a season plays the **same puzzles in the same order**, so that
scores are comparable across agents. The schedule is a pure function of the
season config and the puzzle bank. Puzzles used by any season listed under
``exclude.seasons`` are never drawn again, and ``bank_max_id`` (if set) limits a
season to the bank as it was when the season opened. Two rules exist (``schedule:`` in
the season config):

``mix`` (seasons 1-2): a seeded random draw with a fixed difficulty mix.

1. Split the session size into ``word``/``phrase`` counts by ``phrase_share``,
   then each of those into difficulty tiers by the ``difficulty`` mix
   (largest-remainder rounding; ties go to the harder tier).
2. Fill each (type, tier) bucket by drawing from the bank in a seeded random
   order, skipping puzzles whose category already appears
   ``max_per_category`` times.
3. Shuffle the selected puzzles with the same RNG to get the play order.

``difficulty_ramp`` (season 3): puzzles get harder and harder.

1. The pool is every bank entry with a calibrated reference difficulty
   (``ref_guesses``: mean guesses of a fixed reference player).
2. Slot ``i`` of ``n`` gets the target difficulty
   ``start + (end - start) * (i - 1) / (n - 1)``. The last ``wordplay_slots``
   slots draw from ``wordplay_categories`` (Before & After, Rhyme Time, ...),
   the others from all remaining categories.
3. Slots are filled in order with the unused entry closest to the target
   (ties broken by the seeded RNG), respecting ``max_per_category``.
4. The play order is by increasing ``ref_guesses``.

Maintainers can preview a schedule with::

    python -m arena_server.schedule --season 3
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from arena_server.bank import DIFFICULTIES, PUZZLE_TYPES, BankEntry, load_bank
from arena_server.config import SeasonConfig, load_season


class ScheduleError(RuntimeError):
    """The bank cannot satisfy the season's mix."""


@dataclass(frozen=True)
class ScheduledPuzzle:
    index: int  # 1-based play order
    entry: BankEntry
    season: int

    @property
    def puzzle_id(self) -> str:
        return f"s{self.season}-{self.entry.id}"


def _largest_remainder(total: int, weights: dict[str, float], order: list[str]) -> dict[str, int]:
    """Apportion ``total`` across ``weights``; ties are broken by position in ``order``."""
    raw = {k: total * weights.get(k, 0.0) for k in order}
    counts = {k: math.floor(v) for k, v in raw.items()}
    leftover = total - sum(counts.values())
    for k in sorted(order, key=lambda k: (-round(raw[k] - counts[k], 9), order.index(k)))[:leftover]:
        counts[k] += 1
    return counts


def quotas(config: SeasonConfig) -> dict[tuple[str, str], int]:
    """Number of puzzles per ``(type, difficulty)`` bucket."""
    n_phrase = round(config.puzzles_per_session * config.phrase_share)
    by_type = {"word": config.puzzles_per_session - n_phrase, "phrase": n_phrase}
    tier_order = list(reversed(DIFFICULTIES))  # hard first: ties round towards harder puzzles
    result: dict[tuple[str, str], int] = {}
    for kind in PUZZLE_TYPES:
        for tier, n in _largest_remainder(by_type[kind], config.difficulty, tier_order).items():
            result[(kind, tier)] = n
    return result


def build_schedule(config: SeasonConfig, bank: list[BankEntry], *, home: Path) -> list[ScheduledPuzzle]:
    excluded: set[str] = set()
    for past in config.exclude_seasons:
        excluded.update(p.entry.id for p in season_schedule(past, home=home))

    pool = sorted((e for e in bank if e.id not in excluded), key=lambda e: e.id)
    if config.bank_max_id is not None:
        pool = [e for e in pool if int(e.id) <= config.bank_max_id]
    if config.rule == "difficulty_ramp":
        ramped = _ramp(config, pool)
        return [ScheduledPuzzle(index=i, entry=e, season=config.season) for i, e in enumerate(ramped, start=1)]
    rng = random.Random(config.seed)
    picked: list[BankEntry] = []
    per_category: Counter[str] = Counter()

    for (kind, tier), need in sorted(quotas(config).items()):
        candidates = [e for e in pool if e.type == kind and e.difficulty == tier]
        rng.shuffle(candidates)
        chosen = 0
        for entry in candidates:
            if chosen == need:
                break
            if per_category[entry.category] >= config.max_per_category:
                continue
            picked.append(entry)
            per_category[entry.category] += 1
            chosen += 1
        if chosen < need:
            raise ScheduleError(f"season {config.season}: bank has only {chosen} eligible {tier} {kind}s, need {need}")

    rng.shuffle(picked)
    return [ScheduledPuzzle(index=i, entry=e, season=config.season) for i, e in enumerate(picked, start=1)]


def ramp_targets(config: SeasonConfig) -> list[float]:
    """Target reference difficulty of each slot (``difficulty_ramp`` rule)."""
    assert config.ramp is not None
    n, start, end = config.puzzles_per_session, config.ramp.start, config.ramp.end
    return [start + (end - start) * i / (n - 1) if n > 1 else start for i in range(n)]


def _ramp(config: SeasonConfig, pool: list[BankEntry]) -> list[BankEntry]:
    ramp = config.ramp
    assert ramp is not None
    rng = random.Random(config.seed)
    calibrated = [e for e in pool if e.ref_guesses is not None]
    tiebreak = {e.id: rng.random() for e in calibrated}
    wordplay = set(ramp.wordplay_categories)
    first_wordplay_slot = config.puzzles_per_session - ramp.wordplay_slots
    picked: list[BankEntry] = []
    per_category: Counter[str] = Counter()
    for slot, target in enumerate(ramp_targets(config)):
        want_wordplay = slot >= first_wordplay_slot
        candidates = [
            e
            for e in calibrated
            if (e.category in wordplay) == want_wordplay
            and e not in picked
            and per_category[e.category] < config.max_per_category
        ]
        if not candidates:
            kind = "wordplay" if want_wordplay else "regular"
            raise ScheduleError(f"season {config.season}: no calibrated {kind} puzzle left for slot {slot + 1}")
        best = min(candidates, key=lambda e: (abs((e.ref_guesses or 0.0) - target), tiebreak[e.id]))
        picked.append(best)
        per_category[best.category] += 1
    return sorted(picked, key=lambda e: (e.ref_guesses or 0.0, tiebreak[e.id]))


@lru_cache(maxsize=16)
def _cached_schedule(season: int, home: str) -> tuple[ScheduledPuzzle, ...]:
    config = load_season(season, home=Path(home))
    bank = load_bank(config.bank)
    return tuple(build_schedule(config, bank, home=Path(home)))


def season_schedule(season: int, *, home: Path) -> list[ScheduledPuzzle]:
    """The schedule for ``season`` using configs and bank under ``home``."""
    return list(_cached_schedule(season, str(home.resolve())))


# -- maintainer CLI --------------------------------------------------------


def _print_table(config: SeasonConfig, schedule: list[ScheduledPuzzle], *, hide_answers: bool) -> None:
    print(f"{config.name} (seed {config.seed}) - {len(schedule)} puzzles, max {config.max_guesses} guesses each")
    if config.ramp is not None:
        ramp = config.ramp
        print(f"rule: difficulty_ramp, target reference difficulty {ramp.start:g} -> {ramp.end:g} guesses")
    print(f"bank: {config.bank}")
    print()
    header = ("#", "puzzle", "type", "difficulty", "ref", "category", "answer")
    rows = [
        (
            str(p.index),
            p.puzzle_id,
            p.entry.type,
            p.entry.difficulty,
            "-" if p.entry.ref_guesses is None else f"{p.entry.ref_guesses:.1f}",
            p.entry.category,
            "*" * len(p.entry.answer) if hide_answers else p.entry.answer,
        )
        for p in schedule
    ]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for r in [header, *rows]:
        print("  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=True)).rstrip())
    mix = Counter((p.entry.type, p.entry.difficulty) for p in schedule)
    print()
    print("mix: " + ", ".join(f"{k}/{t}={n}" for (k, t), n in sorted(mix.items())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m arena_server.schedule",
        description="Preview the deterministic puzzle schedule for a season (maintainers only).",
    )
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--home", type=Path, default=Path.cwd(), help="arena checkout (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--hide-answers", action="store_true", help="mask answers in the table")
    args = parser.parse_args(argv)

    config = load_season(args.season, home=args.home)
    schedule = season_schedule(args.season, home=args.home)
    if args.json:
        payload = [
            {
                "index": p.index,
                "puzzle_id": p.puzzle_id,
                "bank_id": p.entry.id,
                "answer": p.entry.answer,
                "type": p.entry.type,
                "category": p.entry.category,
                "difficulty": p.entry.difficulty,
                "ref_guesses": p.entry.ref_guesses,
            }
            for p in schedule
        ]
        json.dump({"season": config.season, "seed": config.seed, "puzzles": payload}, sys.stdout, indent=2)
        print()
    else:
        _print_table(config, schedule, hide_answers=args.hide_answers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
