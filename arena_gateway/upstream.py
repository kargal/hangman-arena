"""HTTP client for the upstream OpenAI Responses API, with retries."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

import httpx

from arena_gateway.config import Provider

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
RETRY_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
)


class UpstreamError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"upstream returned {status}")
        self.status = status
        self.body = body


def _decode(content: bytes) -> Any:
    try:
        return json.loads(content)
    except ValueError:
        return content.decode(errors="replace")[:2000]


def backoff_seconds(attempt: int, retry_after: str | None, *, base: float = 1.0, cap: float = 60.0) -> float:
    if retry_after:
        try:
            return min(cap, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return float(min(cap, base * 2.0**attempt) * random.uniform(0.5, 1.0))


class Upstream:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, sleep: Any = asyncio.sleep) -> None:
        self._transport = transport
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._sleep = sleep

    def _client(self, provider: Provider) -> httpx.AsyncClient:
        client = self._clients.get(provider.name)
        if client is None:
            client = httpx.AsyncClient(
                base_url=provider.base_url,
                timeout=httpx.Timeout(provider.timeout_s, connect=30.0),
                transport=self._transport,
            )
            self._clients[provider.name] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()

    def _headers(self, provider: Provider) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **provider.extra_headers}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        return headers

    async def send(self, provider: Provider, payload: dict[str, Any]) -> tuple[httpx.Response, int]:
        """POST /responses. Returns ``(response, retries)``; for streaming payloads the body is
        not read yet. Raises :class:`UpstreamError` for non-retryable or exhausted failures."""
        client = self._client(provider)
        stream = bool(payload.get("stream"))
        headers = self._headers(provider)
        if stream:
            headers["Accept"] = "text/event-stream"
        attempt = 0
        while True:
            try:
                request = client.build_request("POST", "/responses", json=payload, headers=headers)
                response = await client.send(request, stream=stream)
            except RETRY_EXCEPTIONS as exc:
                if attempt >= provider.max_retries:
                    raise UpstreamError(502, {"error": {"message": f"upstream connection failed: {exc!r}"}}) from exc
                delay = backoff_seconds(attempt, None)
                log.warning("upstream %s: %r, retrying in %.1fs", provider.name, exc, delay)
                await self._sleep(delay)
                attempt += 1
                continue

            if response.status_code < 400:
                return response, attempt
            body = _decode(await response.aread())
            await response.aclose()
            if response.status_code in RETRY_STATUS and attempt < provider.max_retries:
                delay = backoff_seconds(attempt, response.headers.get("retry-after"))
                log.warning("upstream %s: HTTP %d, retrying in %.1fs", provider.name, response.status_code, delay)
                await self._sleep(delay)
                attempt += 1
                continue
            raise UpstreamError(response.status_code, body)
