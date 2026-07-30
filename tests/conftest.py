from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from arena_server.app import create_app
from arena_server.config import Settings

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "test-admin-token"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=ROOT, season=3, results_dir=tmp_path / "results", admin_token=ADMIN)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


def new_session(client: TestClient, **body: object) -> tuple[str, dict[str, str]]:
    r = client.post("/v1/sessions", json={"agent": "test", **body}, headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 201, r.text
    data = r.json()
    return data["session_id"], {"Authorization": f"Bearer {data['token']}"}


@pytest.fixture
def live_server(settings: Settings) -> Iterator[str]:
    """A real HTTP server on a free port (for subprocess-based tests)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
