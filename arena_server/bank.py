"""Loading and validating the puzzle bank (``puzzles/bank.jsonl``)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from hangman_engine import MAX_GUESSES, Puzzle

PUZZLE_TYPES = ("word", "phrase")
DIFFICULTIES = ("easy", "medium", "hard")

#: Answers may contain ASCII letters, single spaces, hyphens, apostrophes and commas,
#: and must start and end with a letter.
ANSWER_RE = re.compile(r"^[A-Za-z](?:[A-Za-z'\-,]|(?<! ) (?! ))*[A-Za-z']$")
ID_RE = re.compile(r"^\d{4}$")


class BankError(ValueError):
    """The puzzle bank is malformed."""


@dataclass(frozen=True)
class BankEntry:
    id: str
    answer: str
    type: str
    category: str
    difficulty: str
    #: Calibrated reference difficulty (mean guesses of the reference player), if measured.
    ref_guesses: float | None = None

    def to_puzzle(self, season: int) -> Puzzle:
        return Puzzle(
            puzzle_id=f"s{season}-{self.id}",
            answer=self.answer,
            category=self.category,
            kind=self.type,
            difficulty=self.difficulty,
            reference_guesses=self.ref_guesses,
        )


def validate_entries(entries: Iterable[BankEntry]) -> list[str]:
    """Return a list of human-readable problems (empty if the bank is valid)."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_answers: dict[str, str] = {}
    for e in entries:
        where = f"entry {e.id!r}"
        if not ID_RE.match(e.id):
            problems.append(f"{where}: id must be four digits")
        if e.id in seen_ids:
            problems.append(f"{where}: duplicate id")
        seen_ids.add(e.id)
        if not ANSWER_RE.match(e.answer):
            problems.append(f"{where}: illegal characters or spacing in answer {e.answer!r}")
        key = " ".join(e.answer.upper().split())
        if key in seen_answers:
            problems.append(f"{where}: duplicate answer {e.answer!r} (also {seen_answers[key]!r})")
        seen_answers[key] = e.id
        if e.type not in PUZZLE_TYPES:
            problems.append(f"{where}: unknown type {e.type!r}")
        elif (e.type == "word" and " " in e.answer) or (e.type == "phrase" and not any(c in e.answer for c in " -")):
            problems.append(f"{where}: type {e.type!r} does not match answer {e.answer!r}")
        if e.difficulty not in DIFFICULTIES:
            problems.append(f"{where}: unknown difficulty {e.difficulty!r}")
        if not e.category.strip():
            problems.append(f"{where}: empty category")
        if e.ref_guesses is not None and not 1.0 <= e.ref_guesses <= MAX_GUESSES:
            problems.append(f"{where}: ref_guesses must be between 1 and {MAX_GUESSES}, got {e.ref_guesses!r}")
    return problems


def load_bank(path: Path, *, validate: bool = True) -> list[BankEntry]:
    """Load ``path`` (JSON Lines). Raises :class:`BankError` on any problem."""
    entries: list[BankEntry] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            entries.append(
                BankEntry(
                    id=str(raw["id"]),
                    answer=raw["answer"],
                    type=raw["type"],
                    category=raw["category"],
                    difficulty=raw["difficulty"],
                    ref_guesses=float(raw["ref_guesses"]) if raw.get("ref_guesses") is not None else None,
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise BankError(f"{path}:{lineno}: cannot parse entry ({exc})") from exc
    if validate:
        problems = validate_entries(entries)
        if problems:
            raise BankError(f"{path}: {len(problems)} problem(s), first: {problems[0]}")
    return entries
