import json
from pathlib import Path

from fastapi.testclient import TestClient

from arena_server.app import create_app
from arena_server.config import Settings
from arena_server.schedule import season_schedule
from tests.conftest import ADMIN, ROOT, new_session

SCHEDULE = season_schedule(3, home=ROOT)


def test_healthz_and_season(client: TestClient) -> None:
    assert client.get("/healthz").json()["season"] == 3
    season = client.get("/v1/season").json()
    assert season["puzzles_per_session"] == 20
    assert season["max_guesses"] == 30


def test_creating_a_session_requires_admin_token(client: TestClient) -> None:
    assert client.post("/v1/sessions", json={}).status_code == 401
    r = client.post("/v1/sessions", json={}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_wrong_season_is_rejected(client: TestClient) -> None:
    r = client.post("/v1/sessions", json={"season": 2}, headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "wrong_season"


def test_session_token_is_scoped_to_its_session(client: TestClient) -> None:
    sid_a, auth_a = new_session(client)
    sid_b, _ = new_session(client)
    assert client.get(f"/v1/sessions/{sid_a}", headers=auth_a).status_code == 200
    assert client.get(f"/v1/sessions/{sid_b}", headers=auth_a).status_code == 401
    assert client.get(f"/v1/sessions/{sid_a}").status_code == 401
    assert client.get("/v1/sessions/ses_missing", headers=auth_a).status_code == 404


def test_next_serves_masked_puzzle_without_answer(client: TestClient) -> None:
    sid, auth = new_session(client)
    body = client.post(f"/v1/sessions/{sid}/next", headers=auth).json()
    puzzle = body["puzzle"]
    first = SCHEDULE[0].entry
    assert puzzle["puzzle_id"] == SCHEDULE[0].puzzle_id
    assert puzzle["answer"] is None
    assert puzzle["category"] == first.category
    assert set(puzzle["mask"]) <= {"_", " ", "-", "'", ","}
    assert first.answer.upper() not in json.dumps(body).upper()
    assert body["session"]["current_index"] == 1


def test_next_requires_finished_puzzle(client: TestClient) -> None:
    sid, auth = new_session(client)
    client.post(f"/v1/sessions/{sid}/next", headers=auth)
    r = client.post(f"/v1/sessions/{sid}/next", headers=auth)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "puzzle_in_progress"


def test_guess_before_next_is_rejected(client: TestClient) -> None:
    sid, auth = new_session(client)
    r = client.post(f"/v1/sessions/{sid}/guess", json={"letter": "e"}, headers=auth)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_active_puzzle"


def test_guess_validation_and_repeats(client: TestClient) -> None:
    sid, auth = new_session(client)
    client.post(f"/v1/sessions/{sid}/next", headers=auth)
    r = client.post(f"/v1/sessions/{sid}/guess", json={"letter": "ab"}, headers=auth)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_guess"
    r = client.post(f"/v1/sessions/{sid}/guess", json={}, headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_request"
    assert client.post(f"/v1/sessions/{sid}/guess", json={"letter": "q"}, headers=auth).status_code == 200
    r = client.post(f"/v1/sessions/{sid}/guess", json={"letter": "Q"}, headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "repeated_guess"
    assert client.get(f"/v1/sessions/{sid}", headers=auth).json()["puzzle"]["guesses"] == 1


def test_full_session_in_schedule_order(client: TestClient, settings: Settings) -> None:
    sid, auth = new_session(client, model="test-model")
    served = []
    for p in SCHEDULE:
        puzzle = client.post(f"/v1/sessions/{sid}/next", headers=auth).json()["puzzle"]
        served.append(puzzle["puzzle_id"])
        body = client.post(f"/v1/sessions/{sid}/solve", json={"answer": p.entry.answer.lower()}, headers=auth).json()
        assert body["result"]["correct"]
        assert body["puzzle"]["answer"] == " ".join(p.entry.answer.upper().split())
    assert served == [p.puzzle_id for p in SCHEDULE]
    assert body["session"]["finished"]
    assert body["summary"]["average_guesses"] == 1.0

    r = client.post(f"/v1/sessions/{sid}/next", headers=auth)
    assert r.json()["error"]["code"] == "session_complete"

    history = client.get(f"/v1/sessions/{sid}/history", headers=auth).json()["puzzles"]
    assert [h["index"] for h in history] == list(range(1, 21))

    season_dir = settings.results_dir / "season-3"
    events = [json.loads(line) for line in (season_dir / f"{sid}.jsonl").read_text().splitlines()]
    assert events[0]["event"] == "session_created"
    assert "token" not in events[0]
    assert events[-1]["event"] == "session_finished"
    assert json.loads((season_dir / f"{sid}.summary.json").read_text())["summary"]["puzzles"] == 20


def test_forfeit_after_30_guesses(client: TestClient) -> None:
    sid, auth = new_session(client)
    client.post(f"/v1/sessions/{sid}/next", headers=auth)
    for i in range(30):
        body = client.post(f"/v1/sessions/{sid}/solve", json={"answer": f"wrong {i}"}, headers=auth).json()
    assert body["puzzle"]["status"] == "forfeited"
    assert body["puzzle"]["score"] == 30
    assert body["puzzle"]["answer"] is not None
    r = client.post(f"/v1/sessions/{sid}/solve", json={"answer": "late"}, headers=auth)
    assert r.json()["error"]["code"] == "no_active_puzzle"
    summary = client.get(f"/v1/sessions/{sid}/summary", headers=auth).json()
    assert summary["scores"] == [30]
    assert summary["summary"]["forfeited"] == 1


def test_sessions_are_restored_after_restart(settings: Settings) -> None:
    with TestClient(create_app(settings)) as first:
        sid, auth = new_session(first)
        first.post(f"/v1/sessions/{sid}/next", headers=auth)
        first.post(f"/v1/sessions/{sid}/guess", json={"letter": "e"}, headers=auth)
        first.post(f"/v1/sessions/{sid}/guess", json={"letter": "e"}, headers=auth)  # rejected, not counted
        before = first.get(f"/v1/sessions/{sid}", headers=auth).json()
    with TestClient(create_app(settings)) as second:
        after = second.get(f"/v1/sessions/{sid}", headers=auth).json()
    assert after == before
    assert after["puzzle"]["guesses"] == 1


def test_admin_can_list_sessions(client: TestClient) -> None:
    new_session(client)
    r = client.get("/v1/sessions", headers={"Authorization": f"Bearer {ADMIN}"})
    assert len(r.json()["sessions"]) == 1


def test_results_dir_is_created(tmp_path: Path) -> None:
    settings = Settings(home=ROOT, season=3, results_dir=tmp_path / "nested" / "results", admin_token=ADMIN)
    with TestClient(create_app(settings)) as c:
        new_session(c)
    assert (tmp_path / "nested" / "results" / "season-3").is_dir()
