from pathlib import Path

from scripts.render_leaderboard import END, START, load_seasons, main, render

ROOT = Path(__file__).resolve().parents[1]

ENTRY_FIELDS = {"model", "agent", "endpoint", "avg_guesses", "ci95", "solve_rate", "learning_slope", "sessions"}


def test_leaderboard_files_are_well_formed() -> None:
    seasons = load_seasons(ROOT / "leaderboard")
    numbers = [s["season"] for s in seasons]
    assert numbers == sorted(set(numbers), reverse=True)
    for season in seasons:
        assert season["status"] in {"in_progress", "final"}
        for e in season["entries"]:
            assert set(e) >= ENTRY_FIELDS
            assert 1 <= e["avg_guesses"] <= season["max_guesses"]
            assert e["agent"] == "Claude Code"
            if season.get("closes"):
                assert season["opens"] <= e["submitted"] <= season["closes"]


def _entry(model: str, avg: float) -> dict[str, object]:
    return {
        "model": model,
        "agent": "Claude Code",
        "endpoint": "test",
        "avg_guesses": avg,
        "ci95": 0.5,
        "solve_rate": 1.0,
        "learning_slope": -0.1,
        "sessions": 5,
    }


def test_render_sorts_by_avg_guesses() -> None:
    season = {
        "season": 9,
        "status": "final",
        "opens": "2030-01-01",
        "closes": "2030-02-01",
        "updated": "2030-02-02",
        "notes": "Synthetic test data.",
        "entries": [_entry("model-b", 7.5), _entry("model-a", 2.25), _entry("model-c", 11.0)],
    }
    table = render([season])
    rows = [line for line in table.splitlines() if line.startswith("| ") and line[2].isdigit()]
    assert [r.split(" | ")[1] for r in rows] == ["model-a", "model-b", "model-c"]
    values = [float(r.split("**")[1]) for r in rows]
    assert values == sorted(values)


def test_empty_season_renders_notes_only() -> None:
    season = {
        "season": 9,
        "status": "in_progress",
        "opens": "2030-01-01",
        "closes": None,
        "notes": "Results are published when the season closes.",
        "entries": [],
    }
    text = render([season])
    assert text.startswith("### Season 9 (in progress, opened 2030-01-01)")
    assert "| # |" not in text
    assert "_Results are published when the season closes._" in text


def test_readme_is_up_to_date() -> None:
    assert main(["--check"]) == 0
    readme = (ROOT / "README.md").read_text()
    assert readme.index(START) < readme.index(END)


def test_check_detects_stale_readme(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(f"# x\n{START}\nold\n{END}\n")
    assert main(["--check", "--readme", str(readme)]) == 1
    assert main(["--readme", str(readme)]) == 0
    assert main(["--check", "--readme", str(readme)]) == 0
