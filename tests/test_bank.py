from collections import Counter
from pathlib import Path

import pytest

from arena_server.bank import BankEntry, BankError, load_bank, validate_entries

ROOT = Path(__file__).resolve().parents[1]
BANK = ROOT / "puzzles" / "bank.jsonl"


@pytest.fixture(scope="module")
def bank() -> list[BankEntry]:
    return load_bank(BANK)


def test_bank_is_valid(bank: list[BankEntry]) -> None:
    assert validate_entries(bank) == []


def test_bank_size_and_mix(bank: list[BankEntry]) -> None:
    assert 1400 <= len(bank) <= 1700
    types = Counter(e.type for e in bank)
    assert 0.25 <= types["phrase"] / len(bank) <= 0.35
    tiers = Counter(e.difficulty for e in bank)
    assert set(tiers) == {"easy", "medium", "hard"}


def test_ids_are_sequential(bank: list[BankEntry]) -> None:
    assert [e.id for e in bank] == [f"{i:04d}" for i in range(1, len(bank) + 1)]


def entry(answer: str, *, id: str = "0001", type: str = "word") -> BankEntry:
    return BankEntry(id=id, answer=answer, type=type, category="Test", difficulty="easy")


@pytest.mark.parametrize(
    "answer",
    ["", "two  spaces", " leading", "trailing ", "digits9", "caf\u00e9", "semi;colon", "-dash", "ques?"],
)
def test_illegal_answers_are_reported(answer: str) -> None:
    kind = "phrase" if " " in answer.strip() else "word"
    assert validate_entries([entry(answer, type=kind)])


@pytest.mark.parametrize("answer", ["Don't Stop Believin'", "Merry-go-round", "Sweet Child o' Mine", "jinx"])
def test_legal_answers_pass(answer: str) -> None:
    kind = "word" if " " not in answer and "-" not in answer else "phrase"
    assert validate_entries([entry(answer, type=kind)]) == []


def test_duplicates_are_reported() -> None:
    problems = validate_entries([entry("Oxbow"), entry("oxbow", id="0002"), entry("teacup", id="0002")])
    assert any("duplicate answer" in p for p in problems)
    assert any("duplicate id" in p for p in problems)


def test_type_mismatch_is_reported() -> None:
    assert validate_entries([entry("rock and roll", type="word")])
    assert validate_entries([entry("teacup", type="phrase")])


def test_load_bank_rejects_broken_lines(tmp_path: Path) -> None:
    path = tmp_path / "bank.jsonl"
    path.write_text('{"id": "0001", "answer": "oxbow"}\n')
    with pytest.raises(BankError):
        load_bank(path)
