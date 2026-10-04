"""OpenAI-compatible HTTP surface in front of the free models on OpenCode Zen."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from . import bridge
from .catalog import OPENAI_FORMATS, CatalogEntry, UpstreamFormat, normalize_model_id
from .config import Settings, get_settings
from .sse import openai_error
from .upstream import ZenClient, ZenError, is_wrong_route

log = logging.getLogger("zen_free_proxy")

TITLE = "Zen Free Proxy"

#: Importable factory for `uvicorn --factory` (used by the CLI under --reload).
APP_FACTORY = "zen_free_proxy.app:create_app"


def create_app(settings: Settings | None = None, client: ZenClient | None = None) -> FastAPI:
    settings = settings or get_settings()
    zen = client or ZenClient(
        settings.zen_base_url,
        settings.api_key,
        timeout=settings.request_timeout,
        connect_timeout=settings.connect_timeout,
        max_retries=settings.max_retries,
        opencode_client="cli" if settings.zen_client_headers else None,
        models_dev_url=settings.models_dev_url,
        models_dev_mirror_url=settings.models_dev_mirror_url,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "zen-free-proxy on %s:%d -> %s (key=%s, client auth=%s, zen client headers=%s, catalog=%s)",
            settings.host,
            settings.port,
            settings.zen_base_url,
            "yes" if settings.api_key else "anonymous",
            "on" if settings.auth_required else "off",
            "on" if settings.zen_client_headers else "off",
            ", ".join(settings.catalog_sources()) or "off",
        )
        await zen.refresh_catalog(settings.catalog_ttl)
        try:
            yield
        finally:
            await zen.aclose()

    app = FastAPI(title=TITLE, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.settings = settings
    app.state.zen = zen

    @app.exception_handler(_BadRequest)
    async def _bad_request_handler(_: Request, exc: _BadRequest) -> JSONResponse:
        return JSONResponse(status_code=400, content=openai_error(str(exc)))

    # ---------------------------------------------------------------- middleware

    @app.middleware("http")
    async def open_auth_and_touch_catalog(request: Request, call_next):
        # Deliberately permissive: the client's Authorization header is never read.
        # Set ZEN_PROXY_ALLOWED_CLIENT_KEYS to require one of your own keys instead.
        if settings.auth_required:
            presented = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
            if presented not in settings.allowed_client_keys:
                return JSONResponse(
                    status_code=401,
                    content=openai_error(
                        "unknown client api key", type_="authentication_error", code="invalid_api_key"
                    ),
                )

        if request.url.path.endswith(("/chat/completions", "/responses", "/systemone")):
            await zen.refresh_catalog(settings.catalog_ttl)
        return await call_next(request)

    # ------------------------------------------------------------------- routes

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "service": TITLE,
            "upstream": settings.zen_base_url,
            "authenticated_upstream": bool(settings.api_key),
            "client_auth_required": settings.auth_required,
            "endpoints": ["/v1/models", "/v1/chat/completions", "/v1/responses", "/v1/systemone", "/healthz"],
            "free_models": zen.catalog.ids(),
        }

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        upstream_ok, detail = await _probe_upstream(zen)
        return JSONResponse(
            status_code=200 if upstream_ok else 503,
            content={
                "status": "ok" if upstream_ok else "degraded",
                "upstream": settings.zen_base_url,
                "upstream_reachable": upstream_ok,
                "detail": detail,
                "free_models": len(zen.catalog),
            },
        )

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [entry.to_openai() for entry in sorted(zen.catalog, key=lambda e: e.id)],
        }

    @app.get("/v1/models/{model_id:path}")
    async def retrieve_model(model_id: str) -> Response:
        entry = _lookup(zen, model_id)
        if entry is None:
            return _model_error(model_id, zen)
        return JSONResponse(entry.to_openai())

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        body = await _json_body(request)
        entry = _lookup(zen, body.get("model"))
        if entry is None:
            return JSONResponse(
                status_code=404,
                content=openai_error(_model_message(zen, body.get("model")), code="model_not_found"),
            )

        if entry.upstream not in OPENAI_FORMATS:
            return JSONResponse(status_code=400, content=openai_error(_route_hint(entry)))

        want_stream = bool(body.get("stream"))
        body["model"] = entry.id
        body.pop("stream_options", None)

        return await _chat_via_zen(zen, entry, body, want_stream)

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        body = await _json_body(request)
        entry = _lookup(zen, body.get("model"))
        if entry is None:
            return JSONResponse(
                status_code=404,
                content=openai_error(_model_message(zen, body.get("model")), code="model_not_found"),
            )
        if entry.upstream is not UpstreamFormat.RESPONSES:
            return JSONResponse(
                status_code=400,
                content=openai_error(_route_hint(entry), code="unsupported_route"),
            )

        body["model"] = entry.id
        want_stream = bool(body.get("stream"))
        try:
            upstream = await zen.post(entry.upstream.value, body, stream=want_stream)
        except ZenError as exc:
            return _zen_error_response(exc)
        return _passthrough_response(upstream)

    @app.post("/v1/systemone")
    async def systemone(request: Request) -> Response:
        body = await _json_body(request)
        entry = _lookup(zen, body.get("model"))
        if entry is None:
            return JSONResponse(
                status_code=404,
                content=openai_error(_model_message(zen, body.get("model")), code="model_not_found"),
            )
        if entry.upstream is not UpstreamFormat.SYSTEMONE:
            return JSONResponse(
                status_code=404,
                content=openai_error(_route_hint(entry), code="model_not_found"),
            )
        body["model"] = entry.id
        try:
            upstream = await zen.post(entry.upstream.value, body)
        except ZenError as exc:
            return _zen_error_response(exc)
        return _passthrough_response(upstream)

    return app


# --------------------------------------------------------------------- handlers


async def _chat_via_zen(
    zen: ZenClient, entry: CatalogEntry, body: dict[str, Any], want_stream: bool
) -> Response:
    """Send a chat request on the route the catalog names, and correct it if wrong.

    The route comes from models.dev, not from a table here, so it can be stale the
    moment Zen moves a model. When the gateway says the model is not on that route,
    the other OpenAI-shaped route is tried and the winner is remembered in the
    catalog, so a wrong guess costs one extra request once instead of a patch.
    """
    order = [entry.upstream, *(fmt for fmt in OPENAI_FORMATS if fmt is not entry.upstream)]
    for index, fmt in enumerate(order):
        last = index == len(order) - 1
        try:
            upstream = await zen.post(fmt.value, _upstream_body(fmt, body, want_stream), stream=want_stream)
        except ZenError as exc:
            if not last and _other_route_may_work(zen, entry, exc):
                log.info("zen will not serve %s on %s, trying the other route", entry.id, fmt.value)
                continue
            return _zen_error_response(exc)
        if index:
            log.info("zen serves %s on %s, correcting the catalog", entry.id, fmt.value)
            zen.catalog.remember_route(entry.id, fmt)
        if fmt is UpstreamFormat.RESPONSES:
            return _responses_as_chat(upstream, entry.id, want_stream)
        return _passthrough_response(upstream)
    raise AssertionError("unreachable")  # pragma: no cover - the last format always returns


def _other_route_may_work(zen: ZenClient, entry: CatalogEntry, exc: ZenError) -> bool:
    """Whether a failed route is worth one try on the other one.

    Zen names the format it wanted when it refuses a route outright, which is
    always worth trying. Some models it accepts and then fails on with a 5xx, and
    that only counts when the route was a name-based guess in the first place: a 5xx
    on a route a source told us about is a real failure, and the retry has already
    happened inside the upstream client.
    """
    if is_wrong_route(exc):
        return True
    return exc.status >= 500 and zen.catalog.route_is_guessed(entry.id)


def _upstream_body(fmt: UpstreamFormat, body: dict[str, Any], want_stream: bool) -> dict[str, Any]:
    if fmt is UpstreamFormat.RESPONSES:
        return bridge.chat_request_to_responses(body)
    if want_stream:
        # Zen only emits a usage block when asked; ask, so downstream cost meters work.
        return {**body, "stream_options": {"include_usage": True}}
    return body


def _responses_as_chat(upstream: httpx.Response, model: str, want_stream: bool) -> Response:
    """Serve a Responses-API model to a chat.completions client."""
    if not want_stream:
        payload = upstream.json()
        if payload.get("error"):
            return _zen_error_response(ZenError(upstream.status_code, payload))
        return JSONResponse(bridge.responses_to_chat_completion(payload, model))

    return StreamingResponse(
        bridge.responses_sse_to_chat_sse(upstream.aiter_bytes(), model),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


def _passthrough_response(upstream: httpx.Response) -> Response:
    request = upstream.request
    if "text/event-stream" not in upstream.headers.get("content-type", "").lower():

        async def single() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            single(),
            status_code=upstream.status_code,
            media_type="application/json",
            headers={"cache-control": "no-cache"},
        )

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        except Exception as exc:
            log.info("stream aborted: %s", exc)
        finally:
            await upstream.aclose()

    headers = {"cache-control": "no-cache", "x-accel-buffering": "no", "x-upstream-url": str(request.url)}
    return StreamingResponse(
        stream(),
        status_code=upstream.status_code,
        media_type="text/event-stream",
        headers=headers,
    )


async def _probe_upstream(zen: ZenClient) -> tuple[bool, str | None]:
    try:
        payload = await zen.get_json("/models")
    except Exception as exc:
        return False, str(exc)
    total = len(payload.get("data") or []) if isinstance(payload, dict) else 0
    return True, f"{total} models upstream"


# ----------------------------------------------------------------------- helpers


def _lookup(zen: ZenClient, model_id: Any) -> CatalogEntry | None:
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    return zen.catalog.get(normalize_model_id(model_id))


def _route_hint(entry: CatalogEntry) -> str:
    route = f"/v1/{entry.upstream.value}"
    return f"{entry.id} is served by zen on {route}, call POST {route} instead"


def _model_message(zen: ZenClient, model_id: Any) -> str:
    if not model_id:
        return "'model' is required"
    name = normalize_model_id(str(model_id))
    served = ", ".join(zen.catalog.ids())
    if zen.catalog.is_paid(name):
        return f"{name} is a paid Zen model; this proxy only serves free models: {served}"
    if zen.catalog.is_blocked(name):
        return f"{name} exists on Zen but is not priced as free; this proxy only serves free models: {served}"
    return f"unknown model {name}; this proxy only serves free models: {served}"


def _model_error(model_id: Any, zen: ZenClient) -> Response:
    return JSONResponse(
        status_code=404, content=openai_error(_model_message(zen, model_id), code="model_not_found")
    )


_FREE_TIER_HINT = (
    "Zen only serves its free tier to requests that carry an account API key. The "
    "official OpenCode client headers are already replayed by this proxy (see "
    "ZEN_PROXY_ZEN_CLIENT_HEADERS) and do not lift this on their own, so the fix is "
    "a key: set ZEN_API_KEY from https://opencode.ai/auth and restart."
)


def _zen_error_response(exc: ZenError) -> Response:
    status = exc.status
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    message = error.get("message") or error.get("detail") or "upstream error"
    err_type = error.get("type") or "upstream_error"
    code = error.get("code")
    headers: dict[str, str] | None = None

    if err_type == "FreeTierError" or "free tier" in str(message).lower():
        message = f"{message}. {_FREE_TIER_HINT}"
        status = 403
        err_type = "free_tier_restricted"
    elif status == 429:
        headers = {"retry-after": str(int(payload.get("retry_after") or 1))}

    return JSONResponse(
        status_code=status if 400 <= status < 600 else 502,
        content=openai_error(message, type_=err_type, code=code),
        headers=headers,
    )


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise _BadRequest(f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise _BadRequest("request body must be a JSON object")
    return body


class _BadRequest(Exception):
    """Raised for a body we cannot even parse, so the client gets a 400 not a 500."""
