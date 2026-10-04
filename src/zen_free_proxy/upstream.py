"""HTTP client for the OpenCode Zen gateway."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any
from uuid import uuid4

import httpx

from .catalog import Catalog, CatalogSource
from .models_dev import (
    MAX_BYTES,
    MODELS_DEV_URL,
    OPENCODE_SNAPSHOT_URL,
    RETRY_FLOOR_SECONDS,
    parse_models_dev,
)

log = logging.getLogger("zen_free_proxy.upstream")

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 529})

#: Zen says its free tier is "only for use from within OpenCode", so upstream
#: traffic is tagged exactly like the official client tags it. These are the only
#: Zen-facing headers the app sends (read out of the compiled binary): the client
#: name, a session id, a per-request id and the user agent, which is
#: ``opencode/${VERSION}``. Replayed verbatim so a proxied request is
#: indistinguishable from one made by the app itself.
OPENCODE_USER_AGENT = "opencode/1.18.23"
PROXY_USER_AGENT = "zen-free-proxy/0.1"


class ZenError(Exception):
    """Zen answered with a non-retryable error status."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self.payload = payload
        super().__init__(f"zen responded {status}: {payload}")

    @property
    def message(self) -> str:
        error = self.payload.get("error") if isinstance(self.payload, dict) else None
        error = error if isinstance(error, dict) else self.payload
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or "")
        return ""


def is_wrong_route(exc: ZenError) -> bool:
    """True when Zen refused the request because the model lives on another route.

    Zen answers ``Model X is not supported for format openai`` when the route is
    wrong, which is the one error that says the catalog needs correcting rather
    than the request.
    """
    return "not supported for format" in exc.message.lower()


class ZenClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        timeout: float = 600.0,
        connect_timeout: float = 15.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        opencode_client: str | None = "cli",
        models_dev_url: str | None = MODELS_DEV_URL,
        models_dev_mirror_url: str | None = OPENCODE_SNAPSHOT_URL,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            transport=transport,
            follow_redirects=True,
        )
        self.catalog = Catalog(fallback_created=int(time.time()))
        self._catalog_lock = asyncio.Lock()
        self._catalog_fetched_at = 0.0
        self._catalog_attempted_at = 0.0
        self._upstream_ids: list[str] = []
        self._sources: dict[str, CatalogSource] = {}
        self._meta_lock = asyncio.Lock()
        self._meta_fetched_at = 0.0
        self._meta_attempted_at = 0.0
        self._meta_source = ""
        self._model_urls = [url for url in (models_dev_url, models_dev_mirror_url) if url]
        # None disables the impersonation and falls back to a plain proxy UA.
        self._opencode_client = opencode_client
        self._session_id = f"ses_{uuid4().hex}"

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ headers

    def headers(self, *, accept: str = "application/json") -> dict[str, str]:
        # Zen reads an absent key and the literal "public" as anonymous traffic; a
        # malformed key is a hard 401, so never forward the client's own header.
        headers = {
            "authorization": f"Bearer {self.api_key or 'public'}",
            "content-type": "application/json",
            "accept": accept,
        }
        if self._opencode_client is None:
            headers["user-agent"] = PROXY_USER_AGENT
            return headers
        headers["user-agent"] = OPENCODE_USER_AGENT
        headers["x-opencode-client"] = self._opencode_client
        headers["x-opencode-session"] = self._session_id
        headers["x-opencode-request"] = f"req_{uuid4().hex}"
        return headers

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    # ----------------------------------------------------------------- requests

    async def post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        stream: bool = False,
    ) -> httpx.Response:
        accept = "text/event-stream" if stream else "application/json"
        request = self.client.build_request(
            "POST",
            self.url(path),
            headers=self.headers(accept=accept),
            json=body,
        )

        last: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            response = await self.client.send(request, stream=stream)
            if response.status_code not in RETRYABLE_STATUS or attempt == self.max_retries:
                if response.status_code >= 400:
                    await response.aread()
                    raise ZenError(response.status_code, _decode(response))
                return response

            last = response
            await response.aclose()
            delay = _retry_delay(response, attempt)
            log.warning("zen %s -> %s, retrying in %.2fs", path, response.status_code, delay)
            await asyncio.sleep(delay)

        raise ZenError(last.status_code if last else 502, None)

    async def get_json(self, path: str) -> Any:
        request = self.client.build_request("GET", self.url(path), headers=self.headers())
        response = await self.client.send(request)
        response.raise_for_status()
        return response.json()

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    # ----------------------------------------------------------------- catalog

    def apply_catalog(
        self,
        upstream_ids: Iterable[str] | None,
        sources: Mapping[str, CatalogSource] | None = None,
    ) -> None:
        """Rebuild the catalog from Zen's model list plus the models.dev snapshot.

        Split out from :meth:`refresh_catalog` so the two sources can be applied
        independently and in tests without any I/O.
        """
        if upstream_ids is not None:
            self._upstream_ids = sorted(set(upstream_ids))
        if sources:
            # Union, not replace: models.dev retires ids it drops, but Zen can keep
            # serving them, and a missing entry beats reverting to no sizes.
            self._sources.update(sources)
        self.catalog.apply(self._upstream_ids, self._sources)

    async def refresh_catalog(self, ttl: float) -> None:
        """Re-read Zen's model list and models.dev so new free models appear.

        Nothing here is hardcoded, so this is the only thing that has to run for a
        newly launched free model to show up. ``ttl`` of 0 primes the catalog once at
        startup and then leaves it alone. A failure keeps the catalog we already
        have: Zen dropping a model out of the payload is no reason to stop serving
        the rest.
        """
        if not self._catalog_due(ttl):
            return
        async with self._catalog_lock:
            if not self._catalog_due(ttl):
                return
            self._catalog_attempted_at = time.time()
            ids = await self._fetch_upstream_ids()
            sources = await self._refresh_sources(ttl) if self._sources_due(ttl) else {}
            if ids is None and not sources:
                return
            self.apply_catalog(ids, sources)
            self._catalog_fetched_at = time.time()
            log.info(
                "catalog refreshed: %d free of %d zen models, %d described by %s",
                len(self.catalog),
                len(self._upstream_ids),
                len(sources),
                self._meta_source or "no catalog source",
            )

    def _catalog_due(self, ttl: float) -> bool:
        return self._due(self._catalog_fetched_at, self._catalog_attempted_at, ttl)

    def _sources_due(self, ttl: float) -> bool:
        return self._due(self._meta_fetched_at, self._meta_attempted_at, ttl)

    @staticmethod
    def _due(fetched_at: float, attempted_at: float, ttl: float) -> bool:
        """Prime once, refresh every ``ttl``, and back off after a failure.

        The backoff is what keeps an unreachable gateway or a 5MB payload from
        turning every inference request into a fetch.
        """
        now = time.time()
        if not attempted_at:
            return True
        if not fetched_at:
            return now - attempted_at >= RETRY_FLOOR_SECONDS  # never primed yet
        if ttl <= 0:
            return False  # primed once, then stay put
        return now - fetched_at >= ttl

    async def _fetch_upstream_ids(self) -> list[str] | None:
        try:
            payload = await self.get_json("/models")
        except Exception as exc:
            log.warning("could not refresh zen model list: %s", exc)
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            # Not a model list. Better to keep serving the catalog we have than to
            # read "no models" into it and take the proxy offline.
            log.warning("zen /models did not answer with a model list, keeping the current catalog")
            return None
        return [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id")]

    # ---------------------------------------------------------------- metadata

    async def _refresh_sources(self, ttl: float) -> dict[str, CatalogSource]:
        """Read what models.dev knows: prices, routes, windows.

        Fails soft: the caller keeps whatever catalog it already had, and models
        without a source still get served off Zen's list and the name heuristics.
        """
        if not self._model_urls:
            return {}
        async with self._meta_lock:
            if not self._sources_due(ttl):
                return {}
            self._meta_attempted_at = time.time()
            for url in self._model_urls:
                try:
                    payload = await self._fetch_json(url)
                except Exception as exc:
                    log.warning("could not read %s: %s", url, exc)
                    continue
                sources = parse_models_dev(payload)
                if sources:
                    self._meta_source = url
                    self._meta_fetched_at = time.time()
                    return sources
                log.warning("%s carries no opencode provider, trying the next source", url)
            return {}

    async def _fetch_json(self, url: str) -> Any:
        request = self.client.build_request(
            "GET",
            url,
            headers={"accept": "application/json", "user-agent": PROXY_USER_AGENT},
        )
        response = await self.client.send(request, stream=True)
        try:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError(f"{url} payload over {MAX_BYTES} bytes")
                chunks.append(chunk)
        finally:
            await response.aclose()
        return json.loads(b"".join(chunks))


def _decode(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    return min(0.75 * (2**attempt), 8.0)


async def iter_lines(response: httpx.Response) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_bytes():
        yield chunk
