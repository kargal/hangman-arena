"""FastAPI app serving the Anthropic Messages API (as used by Claude Code) on top of OpenAI Responses."""

import asyncio
import contextlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from arena_gateway import __version__
from arena_gateway.config import GatewayConfig, load_config
from arena_gateway.requestlog import RequestLog
from arena_gateway.streaming import StreamTranslator, format_sse, iter_sse, replay_message
from arena_gateway.translate import (
    RequestContext,
    anthropic_error,
    build_upstream_request,
    convert_response,
    estimate_tokens,
    log_usage,
    reasoning_tokens,
    upstream_error_message,
)
from arena_gateway.upstream import Upstream, UpstreamError

log = logging.getLogger(__name__)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(anthropic_error(status, message), status_code=status)


def create_app(config: GatewayConfig | None = None, *, upstream: Upstream | None = None) -> FastAPI:
    config = config or load_config()
    upstream = upstream or Upstream()
    request_log = RequestLog(config.log_file)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await upstream.aclose()

    app = FastAPI(title="Hangman Arena gateway", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.config = config

    def authorized(request: Request) -> bool:
        if not config.api_key:
            return True
        presented = request.headers.get("x-api-key") or ""
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            presented = presented or auth[7:].strip()
        return hmac.compare_digest(presented, config.api_key)

    # -- liveness and discovery ------------------------------------------------------

    @app.api_route("/", methods=["GET", "HEAD"])
    @app.api_route("/api/hello", methods=["GET", "HEAD"])  # Claude Code connectivity check
    def hello() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "default_model": config.default_model}

    def _model_object(name: str) -> dict[str, Any]:
        return {"type": "model", "id": name, "display_name": name, "created_at": "2026-01-01T00:00:00Z"}

    @app.get("/v1/models")
    def list_models() -> dict[str, Any]:
        data = [_model_object(name) for name in config.models]
        return {"data": data, "has_more": False, "first_id": data[0]["id"], "last_id": data[-1]["id"]}

    @app.get("/v1/models/{model_id:path}")
    def get_model(model_id: str) -> dict[str, Any]:
        return _model_object(config.resolve(model_id).name)

    # -- messages ------------------------------------------------------------------

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request) -> Response:
        if not authorized(request):
            return _error(401, "invalid gateway API key")
        body = await request.json()
        return JSONResponse({"input_tokens": estimate_tokens(body)})

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        if not authorized(request):
            return _error(401, "invalid gateway API key")
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "request body is not valid JSON")
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            return _error(400, "messages: field required")

        payload, ctx = build_upstream_request(body, config, dict(request.headers))
        started = time.monotonic()
        record: dict[str, Any] = {
            "request_id": ctx.request_id,
            "session": request.headers.get("x-claude-code-session-id"),
            "client_model": ctx.client_model,
            "arena_model": ctx.route.name,
            "upstream_model": ctx.route.upstream_model,
            "provider": ctx.route.provider.name,
            "stream": ctx.stream,
            "messages": len(body.get("messages") or []),
            "input_items": len(payload["input"]),
            "tools": len(payload.get("tools") or []),
            "client_max_tokens": body.get("max_tokens"),
            "max_output_tokens": payload["max_output_tokens"],
            "reasoning_effort": (payload.get("reasoning") or {}).get("effort"),
            "untranslated": sorted(set(ctx.dropped)) or None,
        }
        if config.log_bodies:
            record["request_body"] = body
            record["upstream_payload"] = payload

        try:
            response, retries = await upstream.send(ctx.route.provider, payload)
        except UpstreamError as exc:
            message = upstream_error_message(exc.status, exc.body)
            status = 400 if message.startswith("prompt is too long") else exc.status
            record.update(status=status, error=message, latency_ms=round((time.monotonic() - started) * 1000))
            request_log.write(record)
            return _error(status, message)
        record["retries"] = retries

        if not ctx.stream or ctx.wants_web_search:
            try:
                data = json.loads(await response.aread())
            finally:
                await response.aclose()
            reply = convert_response(data, ctx)
            usage = data.get("usage") or {}
            record.update(
                status=200,
                upstream_response_id=data.get("id"),
                stop_reason=reply["stop_reason"],
                usage=log_usage(usage),
                reasoning_tokens=reasoning_tokens(usage),
                content_blocks=[b["type"] for b in reply["content"]],
                latency_ms=round((time.monotonic() - started) * 1000),
            )
            if ctx.web_search is not None:
                record["web_search"] = ctx.web_search
                record["web_search_calls"] = ctx.web_search["calls"]
            if config.log_bodies:
                record["response_body"] = reply
            request_log.write(record)
            if ctx.stream:
                events = b"".join(format_sse(name, data) for name, data in replay_message(reply))
                return Response(
                    events,
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            return JSONResponse(reply)

        return StreamingResponse(
            _stream(response, ctx, record, started),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _stream(
        response: Any, ctx: RequestContext, record: dict[str, Any], started: float
    ) -> AsyncIterator[bytes]:
        translator = StreamTranslator(ctx)
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        block_types: list[str] = []
        first_token_ms: int | None = None

        async def pump() -> None:
            try:
                async for event in iter_sse(response.aiter_lines()):
                    await queue.put(("event", event))
            except Exception as exc:
                await queue.put(("error", exc))
            finally:
                await queue.put(("end", None))

        task = asyncio.create_task(pump())
        try:
            for name, data in translator.start():
                yield format_sse(name, data)
            while not translator.finished:
                try:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=config.ping_interval_s)
                except asyncio.TimeoutError:
                    yield format_sse("ping", {"type": "ping"})
                    continue
                if kind == "event":
                    out = translator.feed(value)
                elif kind == "error":
                    out = translator.fail(f"upstream stream error: {value!r}", "api_error")
                else:
                    out = translator.end_of_stream()
                for name, data in out:
                    if name == "content_block_start":
                        block_types.append(data["content_block"]["type"])
                    elif name == "content_block_delta" and first_token_ms is None:
                        first_token_ms = round((time.monotonic() - started) * 1000)
                    yield format_sse(name, data)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await response.aclose()
            upstream_response = translator.upstream_response
            usage = upstream_response.get("usage") or {}
            record.update(
                status=200 if translator.error is None else 502,
                error=translator.error,
                upstream_response_id=upstream_response.get("id"),
                stop_reason=translator.stop_reason,
                usage=log_usage(usage),
                reasoning_tokens=reasoning_tokens(usage),
                content_blocks=block_types,
                first_token_ms=first_token_ms,
                latency_ms=round((time.monotonic() - started) * 1000),
                completed=translator.finished and translator.error is None,
            )
            request_log.write(record)

    # -- everything else -------------------------------------------------------------

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
    async def not_found(path: str, request: Request) -> Response:
        log.warning("unhandled %s /%s", request.method, path)
        return _error(404, f"the arena gateway does not implement {request.method} /{path}")

    return app
