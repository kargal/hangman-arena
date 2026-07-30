import itertools
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from arena_server.bank import BankEntry, load_bank
from arena_server.config import ConfigError, SeasonConfig, load_season
from arena_server.schedule import (
    ScheduleError,
    _largest_remainder,
    build_schedule,
    main,
    quotas,
    ramp_targets,
    season_schedule,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def season3() -> SeasonConfig:
    return load_season(3, home=ROOT)


@pytest.fixture(scope="module")
def season2() -> SeasonConfig:
    return load_season(2, home=ROOT)


def test_schedule_is_deterministic(season3: SeasonConfig) -> None:
    bank = load_bank(season3.bank)
    first = build_schedule(season3, bank, home=ROOT)
    second = build_schedule(season3, list(reversed(bank)), home=ROOT)
    assert [p.puzzle_id for p in first] == [p.puzzle_id for p in second]


def test_schedule_size_and_order(season3: SeasonConfig) -> None:
    schedule = season_schedule(3, home=ROOT)
    assert len(schedule) == season3.puzzles_per_session == 20
    assert [p.index for p in schedule] == list(range(1, 21))
    assert all(p.puzzle_id == f"s3-{p.entry.id}" for p in schedule)
    assert len({p.entry.id for p in schedule}) == 20


def test_mix_schedule_matches_quotas(season2: SeasonConfig) -> None:
    assert season2.rule == "mix"
    schedule = season_schedule(2, home=ROOT)
    mix = Counter((p.entry.type, p.entry.difficulty) for p in schedule)
    assert {k: v for k, v in quotas(season2).items() if v} == dict(mix)
    assert sum(1 for p in schedule if p.entry.type == "phrase") == 5


def test_archived_seasons_use_the_bank_as_of_their_opening() -> None:
    for season, max_id in ((1, 900), (2, 1200)):
        assert load_season(season, home=ROOT).bank_max_id == max_id
        assert all(int(p.entry.id) <= max_id for p in season_schedule(season, home=ROOT))


def test_season3_is_a_difficulty_ramp(season3: SeasonConfig) -> None:
    assert season3.rule == "difficulty_ramp" and season3.ramp is not None
    schedule = season_schedule(3, home=ROOT)
    refs = [p.entry.ref_guesses for p in schedule]
    assert all(r is not None for r in refs)
    assert refs == sorted(refs)  # harder and harder
    assert refs[-1] - refs[0] >= 0.5 * (season3.ramp.end - season3.ramp.start)
    wordplay = [p.entry.category in season3.ramp.wordplay_categories for p in schedule]
    assert sum(wordplay) == season3.ramp.wordplay_slots
    assert all(wordplay[-season3.ramp.wordplay_slots + 2 :])  # the hardest puzzles are wordplay


def test_ramp_targets_are_linear(season3: SeasonConfig) -> None:
    assert season3.ramp is not None
    targets = ramp_targets(season3)
    assert len(targets) == 20
    assert targets[0] == season3.ramp.start
    assert targets[-1] == pytest.approx(season3.ramp.end)
    steps = {round(b - a, 9) for a, b in itertools.pairwise(targets)}
    assert len(steps) == 1


def _ramp_config(**ramp: object) -> SeasonConfig:
    data = {
        "season": 9,
        "bank": "puzzles/bank.jsonl",
        "seed": 5,
        "puzzles_per_session": 4,
        "schedule": "difficulty_ramp",
        "ramp": {"start": 2.0, "end": 8.0, "wordplay_slots": 1, "wordplay_categories": ["Rhyme Time"]} | ramp,
    }
    return SeasonConfig.from_dict(data, home=ROOT)


def _entry(id: str, ref: float | None, category: str = "Animals") -> BankEntry:
    return BankEntry(id=id, answer=f"x{id}", type="word", category=category, difficulty="easy", ref_guesses=ref)


def test_ramp_picks_closest_calibrated_puzzles() -> None:
    config = _ramp_config()  # targets 2, 4, 6, 8; last slot wordplay
    bank = [
        _entry("0001", 2.5, "Birds"),
        _entry("0002", 3.5, "Food"),
        _entry("0003", 6.0, "Tools"),
        _entry("0004", 9.0, "Rhyme Time"),
        _entry("0005", 7.5, "Colors"),  # closest to 8, but not wordplay
        _entry("0006", None, "Words"),  # not calibrated
        _entry("0007", 1.0, "Rhyme Time"),
    ]
    schedule = build_schedule(config, bank, home=ROOT)
    assert [p.entry.id for p in schedule] == ["0001", "0002", "0003", "0004"]
    assert [p.puzzle_id for p in schedule] == ["s9-0001", "s9-0002", "s9-0003", "s9-0004"]


def test_ramp_orders_by_reference_difficulty() -> None:
    config = _ramp_config(wordplay_slots=0, wordplay_categories=[])
    bank = [_entry("0001", 8.0, "A"), _entry("0002", 2.0, "B"), _entry("0003", 6.5, "C"), _entry("0004", 3.0, "D")]
    refs = [p.entry.ref_guesses for p in build_schedule(config, bank, home=ROOT)]
    assert refs == [2.0, 3.0, 6.5, 8.0]


def test_ramp_needs_enough_calibrated_puzzles() -> None:
    with pytest.raises(ScheduleError):
        build_schedule(_ramp_config(), [_entry("0001", 2.0), _entry("0002", None)], home=ROOT)


def test_bad_ramp_is_rejected() -> None:
    with pytest.raises(ConfigError):
        _ramp_config(start=9.0, end=2.0)


def test_no_reuse_across_seasons() -> None:
    ids = [{p.entry.id for p in season_schedule(s, home=ROOT)} for s in (1, 2, 3)]
    assert not ids[0] & ids[1]
    assert not ids[2] & (ids[0] | ids[1])


def test_category_cap(season3: SeasonConfig) -> None:
    counts = Counter(p.entry.category for p in season_schedule(3, home=ROOT))
    assert max(counts.values()) <= season3.max_per_category


def test_seed_changes_mix_schedule(season2: SeasonConfig) -> None:
    bank = load_bank(season2.bank)
    other = build_schedule(replace(season2, seed=season2.seed + 1), bank, home=ROOT)
    assert [p.entry.id for p in other] != [p.entry.id for p in season_schedule(2, home=ROOT)]


def test_largest_remainder() -> None:
    order = ["hard", "medium", "easy"]
    assert _largest_remainder(6, {"easy": 0.1, "medium": 0.3, "hard": 0.6}, order) == {
        "hard": 4,
        "medium": 2,
        "easy": 0,
    }
    assert sum(_largest_remainder(14, {"easy": 0.1, "medium": 0.3, "hard": 0.6}, order).values()) == 14


def test_bad_difficulty_mix_is_rejected() -> None:
    data = {
        "season": 9,
        "bank": "puzzles/bank.jsonl",
        "seed": 1,
        "puzzles_per_session": 20,
        "mix": {"phrase_share": 0.3, "difficulty": {"easy": 0.5, "hard": 0.4}},
    }
    with pytest.raises(ConfigError):
        SeasonConfig.from_dict(data, home=ROOT)


def test_cli_prints_schedule(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--season", "3", "--home", str(ROOT)]) == 0
    out = capsys.readouterr().out
    for p in season_schedule(3, home=ROOT):
        assert p.puzzle_id in out
        assert p.entry.answer in out


def test_cli_hide_answers(capsys: pytest.CaptureFixture[str]) -> None:
    main(["--season", "3", "--home", str(ROOT), "--hide-answers"])
    out = capsys.readouterr().out
    first = season_schedule(3, home=ROOT)[0]
    assert first.puzzle_id in out
    assert first.entry.answer not in out
