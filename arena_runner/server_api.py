"""Minimal client for the runner's use of the arena server (admin + status)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


class ServerError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionHandle:
    session_id: str
    token: str
    season: int
    total: int


class ArenaServer:
    def __init__(
        self, url: str, admin_token: str, *, timeout: float = 30.0, transport: httpx.BaseTransport | None = None
    ):
        self.url = url.rstrip("/")
        self._admin_token = admin_token
        self._http = httpx.Client(base_url=self.url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _check(self, response: httpx.Response) -> dict[str, Any]:
        if response.is_error:
            try:
                message = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = response.text[:200]
            raise ServerError(
                f"{response.request.method} {response.request.url.path}: {response.status_code} {message}"
            )
        body: dict[str, Any] = response.json()
        return body

    def wait_until_healthy(self, timeout: float = 60.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self._check(self._http.get("/healthz"))
            except (httpx.HTTPError, ServerError) as exc:
                if time.monotonic() > deadline:
                    raise ServerError(
                        f"arena server at {self.url} did not become healthy within {timeout:.0f}s"
                    ) from exc
                time.sleep(1.0)

    def create_session(self, *, agent: str, model: str | None, run_id: str, season: int | None) -> SessionHandle:
        body = self._check(
            self._http.post(
                "/v1/sessions",
                json={"agent": agent, "model": model, "run_id": run_id, "season": season},
                headers={"Authorization": f"Bearer {self._admin_token}"},
            )
        )
        return SessionHandle(body["session_id"], body["token"], int(body["season"]), int(body["total"]))

    def _session_get(self, handle: SessionHandle, path: str = "") -> dict[str, Any]:
        return self._check(
            self._http.get(
                f"/v1/sessions/{handle.session_id}{path}", headers={"Authorization": f"Bearer {handle.token}"}
            )
        )

    def status(self, handle: SessionHandle) -> dict[str, Any]:
        return self._session_get(handle)

    def summary(self, handle: SessionHandle) -> dict[str, Any]:
        return self._session_get(handle, "/summary")

    def history(self, handle: SessionHandle) -> dict[str, Any]:
        return self._session_get(handle, "/history")
