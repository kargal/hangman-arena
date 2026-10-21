"""Results database and live standings."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from arena_server.app import create_app
from arena_server.config import Settings
from arena_server.results_db import (
    PuzzleRow,
    ResultsDB,
    default_label,
    normalize_url,
    parse_ts,
    puzzle_results_table,
    sessions_table,
    standings_notice,
)
from arena_server.schedule import season_schedule
from tests.conftest import ADMIN, ROOT, new_session

SCHEDULE = season_schedule(3, home=ROOT)


def play(client: TestClient, sid: str, auth: dict[str, str], wrong: int) -> dict[str, object]:
    """Finish the next puzzle with ``wrong`` wrong solve attempts before the right one."""
    index = client.get(f"/v1/sessions/{sid}", headers=auth).json()["session"]["completed"]
    client.post(f"/v1/sessions/{sid}/next", headers=auth)
    for i in range(wrong):
        body = client.post(f"/v1/sessions/{sid}/solve", json={"answer": f"wrong {i}"}, headers=auth).json()
        assert "standings" not in body
    answer = SCHEDULE[index].entry.answer
    body: dict[str, object] = client.post(f"/v1/sessions/{sid}/solve", json={"answer": answer}, headers=auth).json()
    return body


def test_puzzle_end_response_has_standings(client: TestClient) -> None:
    sid, auth = new_session(client, agent="claude_code", model="model-x")
    body = play(client, sid, auth, wrong=2)
    standings = body["standings"]
    assert isinstance(standings, dict)
    assert standings["season"] == 3
    assert standings["as_of"].endswith("Z")
    assert standings["notice"] == "You are #1 of 1."
    assert standings["entries"] == [
        {
            "rank": 1,
            "label": "model-x (Claude Code)",
            "model": "model-x",
            "agent": "claude_code",
            "completed": 1,
            "average_guesses": 3.0,
            "you": True,
        }
    ]


def test_standings_rank_all_sessions_of_the_season(client: TestClient) -> None:
    a, auth_a = new_session(client, agent="claude_code", model="model-a")
    b, auth_b = new_session(client, agent="codex", model="model-b", label="Team B")
    c, auth_c = new_session(client, agent="claude_code", model="model-c")
    new_session(client, model="model-d")  # no finished puzzle: not listed
    play(client, a, auth_a, wrong=3)  # 4
    play(client, b, auth_b, wrong=0)  # 1
    play(client, b, auth_b, wrong=2)  # 3 -> avg 2.0 over 2
    play(client, c, auth_c, wrong=1)  # 2 -> avg 2.0 over 1

    body = client.get(f"/v1/sessions/{a}/standings", headers=auth_a).json()
    assert body["session"]["session_id"] == a
    entries = body["standings"]["entries"]
    assert [(e["rank"], e["label"], e["completed"], e["average_guesses"], e["you"]) for e in entries] == [
        (1, "Team B", 2, 2.0, False),  # same average, more puzzles finished
        (2, "model-c (Claude Code)", 1, 2.0, False),
        (3, "model-a (Claude Code)", 1, 4.0, True),
    ]
    # the token decides which entry is "you"
    entries = client.get(f"/v1/sessions/{b}/standings", headers=auth_b).json()["standings"]["entries"]
    assert [e["you"] for e in entries] == [True, False, False]


CATCH_UP = " You may need some extra work in the next puzzles to catch up."


def test_notice_on_place_and_gap_to_the_leader(client: TestClient) -> None:
    a, auth_a = new_session(client, model="model-a")
    b, auth_b = new_session(client, model="model-b")
    c, auth_c = new_session(client, model="model-c")
    play(client, b, auth_b, wrong=0)  # 1
    play(client, c, auth_c, wrong=2)  # 3
    body = play(client, a, auth_a, wrong=4)  # 5
    assert body["standings"]["notice"] == (  # type: ignore[index]
        "You are #3 of 3 - the leader averages 4.0 fewer guesses per puzzle." + CATCH_UP
    )
    body = play(client, a, auth_a, wrong=0)  # 5, 1 -> 3.0
    assert body["standings"]["notice"] == (  # type: ignore[index]
        "You are #2 of 3 - the leader averages 2.0 fewer guesses per puzzle." + CATCH_UP
    )
    # every session sees its own place
    body = client.get(f"/v1/sessions/{b}/standings", headers=auth_b).json()
    assert body["standings"]["notice"] == "You are #1 of 3."
    body = client.get(f"/v1/sessions/{c}/standings", headers=auth_c).json()
    assert body["standings"]["notice"] == "You are #3 of 3 - the leader averages 2.0 fewer guesses per puzzle." + (
        CATCH_UP
    )
    # no finished puzzle yet: not ranked, no notice
    d, auth_d = new_session(client, model="model-d")
    assert client.get(f"/v1/sessions/{d}/standings", headers=auth_d).json()["standings"]["notice"] is None


def test_notice_after_every_puzzle_and_at_the_end(client: TestClient) -> None:
    leader, auth_l = new_session(client, model="leader")
    play(client, leader, auth_l, wrong=0)
    sid, auth = new_session(client, model="model-x")
    notices = []
    for p in SCHEDULE:
        client.post(f"/v1/sessions/{sid}/next", headers=auth)
        client.post(f"/v1/sessions/{sid}/solve", json={"answer": "wrong"}, headers=auth)
        body = client.post(f"/v1/sessions/{sid}/solve", json={"answer": p.entry.answer}, headers=auth).json()
        notices.append(body["standings"]["notice"])
    assert len(notices) == 20
    assert notices[:-1] == ["You are #2 of 2 - the leader averages 1.0 fewer guesses per puzzle." + CATCH_UP] * 19
    # nothing left to play: no catch-up sentence
    assert notices[-1] == "You are #2 of 2 - the leader averages 1.0 fewer guesses per puzzle."


def test_notice_wording() -> None:
    assert standings_notice(1, 6, 4.5, 4.5, 10) == "You are #1 of 6."
    assert standings_notice(1, 1, 3.0, 3.0, 0) == "You are #1 of 1."
    assert standings_notice(2, 6, 5.25, 3.45, 12) == (
        "You are #2 of 6 - the leader averages 1.8 fewer guesses per puzzle." + CATCH_UP
    )
    assert standings_notice(4, 6, 3.5, 3.45, 3) == (
        "You are #4 of 6 - the leader averages 0.05 fewer guesses per puzzle." + CATCH_UP
    )
    # rounded once from the exact averages: 213/11 - 196/11 = 1.545... -> "1.5" (not 19.364 - 17.818 = 1.546 -> 1.55)
    assert standings_notice(2, 6, 213 / 11, 196 / 11, 9) == (
        "You are #2 of 6 - the leader averages 1.5 fewer guesses per puzzle." + CATCH_UP
    )
    assert standings_notice(2, 6, 3.004, 3.0, 9) == (
        "You are #2 of 6 - the leader has the same average over more finished puzzles." + CATCH_UP
    )
    assert standings_notice(2, 2, 3.0, 3.0, 5) == (
        "You are #2 of 2 - the leader has the same average over more finished puzzles." + CATCH_UP
    )
    assert standings_notice(3, 6, 6.0, 4.0, 0) == "You are #3 of 6 - the leader averages 2.0 fewer guesses per puzzle."


def test_equal_entries_share_a_rank(client: TestClient) -> None:
    a, auth_a = new_session(client, model="model-a")
    b, auth_b = new_session(client, model="model-b")
    play(client, a, auth_a, wrong=1)
    body = play(client, b, auth_b, wrong=1)
    assert [e["rank"] for e in body["standings"]["entries"]] == [1, 1]  # type: ignore[index]


def test_standings_endpoint_requires_the_session_token(client: TestClient) -> None:
    sid, auth = new_session(client)
    _, other_auth = new_session(client)
    assert client.get(f"/v1/sessions/{sid}/standings").status_code == 401
    assert client.get(f"/v1/sessions/{sid}/standings", headers=other_auth).status_code == 401
    r = client.get(f"/v1/sessions/{sid}/standings", headers=auth)
    assert r.status_code == 200
    assert r.json()["standings"]["entries"] == []


def test_forfeit_is_recorded_as_30(client: TestClient, settings: Settings) -> None:
    sid, auth = new_session(client)
    client.post(f"/v1/sessions/{sid}/next", headers=auth)
    for i in range(30):
        body = client.post(f"/v1/sessions/{sid}/solve", json={"answer": f"wrong {i}"}, headers=auth).json()
    assert body["puzzle"]["status"] == "forfeited"
    assert body["standings"]["entries"][0]["average_guesses"] == 30.0
    db = ResultsDB(settings.database_url)
    assert db.scores(sid) == [30]


def test_database_rows(client: TestClient, settings: Settings) -> None:
    sid, auth = new_session(client, agent="claude_code", model="model-x", run_id="run-1")
    for p in SCHEDULE:
        client.post(f"/v1/sessions/{sid}/next", headers=auth)
        client.post(f"/v1/sessions/{sid}/solve", json={"answer": p.entry.answer}, headers=auth)
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        session = conn.execute(select(sessions_table)).one()
        rows = conn.execute(select(puzzle_results_table).order_by(puzzle_results_table.c.puzzle_index)).all()
    assert (session.session_id, session.season, session.run_id, session.total) == (sid, 3, "run-1", 20)
    assert session.finished_at is not None
    assert [r.puzzle_index for r in rows] == list(range(1, 21))
    assert [r.puzzle_id for r in rows] == [p.puzzle_id for p in SCHEDULE]
    assert {(r.status, r.score) for r in rows} == {("solved", 1)}
    assert (settings.results_dir / "arena.db").exists()


def test_database_is_rebuilt_from_the_event_log(settings: Settings) -> None:
    with TestClient(create_app(settings)) as first:
        sid, auth = new_session(first, model="model-x")
        play(first, sid, auth, wrong=4)
        play(first, sid, auth, wrong=0)
        first.post(f"/v1/sessions/{sid}/next", headers=auth)  # third puzzle in progress
    (settings.results_dir / "arena.db").unlink()
    with TestClient(create_app(settings)) as second:
        entries = second.get(f"/v1/sessions/{sid}/standings", headers=auth).json()["standings"]["entries"]
        assert [(e["completed"], e["average_guesses"], e["label"]) for e in entries] == [(2, 3.0, "model-x (test)")]
    with TestClient(create_app(settings)) as third:  # restoring again does not duplicate rows
        assert ResultsDB(settings.database_url).scores(sid) == [5, 1]
        third.get("/healthz")


def test_standings_limit_keeps_the_requesting_session(settings: Settings) -> None:
    db = ResultsDB(settings.database_url)
    db.create_schema()
    for i in range(30):
        db.record_session(
            session_id=f"ses_{i:02d}",
            season=3,
            label=f"entry {i}",
            agent="test",
            model=None,
            run_id=None,
            total=20,
            created_at="2026-10-21T00:00:00Z",
        )
        db.record_puzzle(
            session_id=f"ses_{i:02d}",
            season=3,
            puzzle=PuzzleRow(1, "s3-0001", "solved", i + 1, parse_ts("2026-10-21T00:01:00Z")),
            session_finished=False,
        )
    standings = db.standings(3, session_id="ses_29")
    # the notice counts every ranked entry, not only the ones listed
    assert standings["notice"] == "You are #30 of 30 - the leader averages 29.0 fewer guesses per puzzle." + CATCH_UP
    entries = standings["entries"]
    assert len(entries) == 26
    assert entries[-1] == {
        "rank": 30,
        "label": "entry 29",
        "model": None,
        "agent": "test",
        "completed": 1,
        "average_guesses": 30.0,
        "you": True,
    }
    assert [e["rank"] for e in entries[:25]] == list(range(1, 26))
    assert db.standings(4)["entries"] == []


def test_database_errors_do_not_fail_moves(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    db = client.app.state.db  # type: ignore[attr-defined]

    def broken(*args: object, **kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("database is down"))

    sid, auth = new_session(client)
    monkeypatch.setattr(db, "standings", broken)
    monkeypatch.setattr(db, "record_puzzle", broken)
    body = play(client, sid, auth, wrong=0)
    assert body["puzzle"]["status"] == "solved"  # type: ignore[index]
    assert "standings" not in body
    r = client.get(f"/v1/sessions/{sid}/standings", headers=auth)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "standings_unavailable"


def test_db_url_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARENA_HOME", str(ROOT))
    monkeypatch.setenv("ARENA_RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("ARENA_ADMIN_TOKEN", ADMIN)
    monkeypatch.delenv("ARENA_DB_URL", raising=False)
    assert Settings.from_env().database_url == f"sqlite:///{tmp_path / 'arena.db'}"
    monkeypatch.setenv("ARENA_DB_URL", "postgresql://arena:pw@db:5432/arena")
    assert Settings.from_env().database_url == "postgresql://arena:pw@db:5432/arena"


def test_url_normalisation_and_labels() -> None:
    assert normalize_url("postgresql://u:p@db/arena").drivername == "postgresql+psycopg"
    assert normalize_url("postgres://u:p@db/arena").drivername == "postgresql+psycopg"
    assert normalize_url("postgresql+psycopg2://u:p@db/arena").drivername == "postgresql+psycopg2"
    assert normalize_url("sqlite:///x.db").drivername == "sqlite"
    assert (
        ResultsDB("postgresql://arena:secret@db:5432/arena").safe_url == "postgresql+psycopg://arena:***@db:5432/arena"
    )
    assert default_label("claude_code", "m") == "m (Claude Code)"
    assert default_label("my_agent", None) == "unknown model (my_agent)"


@pytest.mark.skipif(not os.environ.get("ARENA_TEST_POSTGRES_URL"), reason="set ARENA_TEST_POSTGRES_URL to run")
def test_postgres_round_trip(tmp_path: Path) -> None:
    url = os.environ["ARENA_TEST_POSTGRES_URL"]
    with ResultsDB(url).engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS puzzle_results, sessions"))
    settings = Settings(home=ROOT, season=3, results_dir=tmp_path, admin_token=ADMIN, db_url=url)
    with TestClient(create_app(settings)) as c:
        a, auth_a = new_session(c, model="model-a")
        b, auth_b = new_session(c, model="model-b")
        play(c, a, auth_a, wrong=1)
        play(c, b, auth_b, wrong=0)
        body = play(c, a, auth_a, wrong=0)
        entries = body["standings"]["entries"]  # type: ignore[index]
        assert [(e["label"], e["average_guesses"], e["you"]) for e in entries] == [
            ("model-b (test)", 1.0, False),
            ("model-a (test)", 1.5, True),
        ]
        assert body["standings"]["notice"] == (  # type: ignore[index]
            "You are #2 of 2 - the leader averages 0.5 fewer guesses per puzzle." + CATCH_UP
        )
