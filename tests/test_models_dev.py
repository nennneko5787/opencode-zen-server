from __future__ import annotations

import httpx
import pytest

from zen_free_proxy.catalog import UpstreamFormat
from zen_free_proxy.config import Settings
from zen_free_proxy.models_dev import MODELS_DEV_URL, OPENCODE_SNAPSHOT_URL, parse_models_dev
from zen_free_proxy.upstream import ZenClient

#: Trimmed shape of models.dev api.json for the provider we care about.
PAYLOAD = {
    "anthropic": {"models": {"some-claude": {"id": "some-claude", "cost": {"input": 3, "output": 15}}}},
    "opencode": {
        "id": "opencode",
        "npm": "@ai-sdk/openai-compatible",
        "models": {
            "a-free": {
                "id": "a-free",
                "name": "A Free",
                "reasoning": True,
                "cost": {"input": 0, "output": 0},
                "limit": {"context": 1048576, "input": 524288, "output": 524288},
                "modalities": {"input": ["text", "image", "video"], "output": ["text"]},
            },
            "b-free": {
                "id": "b-free",
                "name": "B Free",
                "cost": {"input": 0, "output": 0},
                "limit": {"context": 200000, "input": 160000, "output": 32000},
                "modalities": {"input": ["text"], "output": ["text"]},
                "provider": {"npm": "@ai-sdk/openai"},
            },
            "paid-looking-free": {
                "id": "paid-looking-free",
                "cost": {"input": 0, "output": 0},
                "limit": {"context": 400000, "output": 64000},
            },
            "priced-free-looking": {"id": "priced-free-looking", "cost": {"input": 1, "output": 3}},
            "unpriced": {"id": "unpriced", "limit": {"context": 262144}},
            "mystery-free": {"id": "mystery-free"},
        },
    },
}


class TestFreeDetection:
    def test_free_means_price_zero(self):
        # opencode's own rule for the anonymous tier: cost.input === 0.
        assert parse_models_dev(PAYLOAD)["a-free"].free is True
        assert parse_models_dev(PAYLOAD)["b-free"].free is True

    def test_a_free_looking_name_that_costs_money_is_not_free(self):
        assert parse_models_dev(PAYLOAD)["priced-free-looking"].free is False

    def test_a_paid_looking_name_can_be_free(self):
        assert parse_models_dev(PAYLOAD)["paid-looking-free"].free is True

    def test_no_price_is_not_free(self):
        assert parse_models_dev(PAYLOAD)["unpriced"].free is False
        assert parse_models_dev(PAYLOAD)["mystery-free"].free is False

    def test_priced_separates_costs_money_from_nobody_said(self):
        sources = parse_models_dev(PAYLOAD)
        assert sources["priced-free-looking"].priced is True
        assert sources["unpriced"].priced is False
        assert sources["mystery-free"].priced is False

    def test_paid_models_are_kept_so_they_can_be_named(self):
        sources = parse_models_dev(PAYLOAD)
        assert sources["priced-free-looking"].free is False
        assert "some-claude" not in sources  # only the opencode provider


class TestRoute:
    def test_openai_sdk_package_means_the_responses_route(self):
        assert parse_models_dev(PAYLOAD)["b-free"].route is UpstreamFormat.RESPONSES

    def test_provider_default_means_the_chat_route(self):
        assert parse_models_dev(PAYLOAD)["a-free"].route is UpstreamFormat.CHAT

    def test_formats_this_proxy_cannot_speak_are_left_unresolved(self):
        payload = {
            "opencode": {"npm": "@ai-sdk/anthropic", "models": {"c": {"cost": {"input": 0, "output": 0}}}}
        }
        assert parse_models_dev(payload)["c"].route is None


class TestMeta:
    def test_reads_window_and_capabilities(self):
        meta = parse_models_dev(PAYLOAD)["a-free"].meta
        assert meta.name == "A Free"
        assert meta.context == 1048576
        assert meta.max_input == 524288
        assert meta.output == 524288
        assert meta.modalities == ("text", "image", "video")
        assert meta.reasoning is True

    def test_picks_up_models_this_proxy_has_never_seen(self):
        # The whole point of asking models.dev instead of shipping a table.
        payload = {
            "opencode": {"models": {"brand-new-free": {"cost": {"input": 0}, "limit": {"context": 128000}}}}
        }
        source = parse_models_dev(payload)["brand-new-free"]
        assert source.free is True
        assert source.meta.context == 128000

    def test_missing_fields_become_absent_not_zero(self):
        meta = parse_models_dev(PAYLOAD)["mystery-free"].meta
        assert meta.name == "mystery-free"
        assert meta.context is None
        assert meta.output is None
        assert meta.modalities == ()
        assert meta.reasoning is False

    @pytest.mark.parametrize("payload", [None, [], "nope", {}, {"opencode": []}, {"opencode": {}}])
    def test_unusable_payloads_yield_nothing(self, payload):
        assert parse_models_dev(payload) == {}

    def test_ignores_junk_inside_limit(self):
        payload = {"opencode": {"models": {"weird-free": {"limit": {"context": "big", "output": 0}}}}}
        meta = parse_models_dev(payload)["weird-free"].meta
        assert meta.context is None
        assert meta.output is None


def _client(
    handler, *, models_dev_url=MODELS_DEV_URL, models_dev_mirror_url=OPENCODE_SNAPSHOT_URL
) -> ZenClient:
    return ZenClient(
        "https://zen.test/v1",
        None,
        timeout=5,
        connect_timeout=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
        models_dev_url=models_dev_url,
        models_dev_mirror_url=models_dev_mirror_url,
    )


ZEN_MODELS = {
    "object": "list",
    "data": [{"id": model_id} for model_id in ("a-free", "b-free", "priced-free-looking", "unpriced")],
}


class TestRefresh:
    @pytest.mark.asyncio
    async def test_both_sources_reach_the_catalog(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.host == "models.dev":
                return httpx.Response(200, json=PAYLOAD)
            return httpx.Response(200, json=ZEN_MODELS)

        zen = _client(handler)
        await zen.refresh_catalog(ttl=900)

        assert seen == ["https://zen.test/v1/models", MODELS_DEV_URL]
        # free set from price, route from the SDK package, windows from limit
        assert zen.catalog.ids() == ["a-free", "b-free"]
        assert zen.catalog.get("b-free").upstream is UpstreamFormat.RESPONSES
        assert zen.catalog.get("a-free").meta.context == 1048576
        assert zen.catalog.is_paid("priced-free-looking") is True
        assert zen.catalog.is_blocked("priced-free-looking") is True

    @pytest.mark.asyncio
    async def test_paid_models_are_named_but_not_served(self):
        zen = _client(
            lambda request: httpx.Response(
                200, json=PAYLOAD if request.url.host == "models.dev" else ZEN_MODELS
            )
        )
        await zen.refresh_catalog(ttl=900)
        assert zen.catalog.ids() == ["a-free", "b-free"]
        assert zen.catalog.upstream_ids() == ["a-free", "b-free", "priced-free-looking", "unpriced"]

    @pytest.mark.asyncio
    async def test_second_call_is_cached(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=PAYLOAD if request.url.host == "models.dev" else ZEN_MODELS)

        zen = _client(handler)
        await zen.refresh_catalog(ttl=900)
        await zen.refresh_catalog(ttl=900)
        assert calls == 2  # one per source, once

    @pytest.mark.asyncio
    async def test_ttl_zero_primes_once_and_then_leaves_it_alone(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=PAYLOAD if request.url.host == "models.dev" else ZEN_MODELS)

        zen = _client(handler)
        for _ in range(3):
            await zen.refresh_catalog(ttl=0)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_disabled_lookup_falls_back_to_names_and_zen(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host != "zen.test":
                raise AssertionError("must not fetch a catalog source")
            return httpx.Response(200, json=ZEN_MODELS)

        zen = _client(handler, models_dev_url=None, models_dev_mirror_url=None)
        await zen.refresh_catalog(ttl=900)
        # No prices to read, so the naming heuristic decides, and what it cannot
        # vouch for is refused rather than guessed at.
        assert zen.catalog.ids() == ["a-free", "b-free"]
        assert zen.catalog.is_paid("priced-free-looking") is False  # unknown price, not a known paid model
        assert zen.catalog.is_blocked("priced-free-looking") is True
        assert zen.catalog.is_blocked("unpriced") is True
        assert all(entry.meta is None for entry in zen.catalog)  # no windows without a source

    @pytest.mark.asyncio
    async def test_mirror_serves_the_catalog_when_the_first_source_is_down(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            seen.append(url)
            if url == MODELS_DEV_URL:
                return httpx.Response(503)
            if url == OPENCODE_SNAPSHOT_URL:
                return httpx.Response(200, json=PAYLOAD)  # same payload, opencode's copy
            return httpx.Response(200, json=ZEN_MODELS)

        zen = _client(handler)
        await zen.refresh_catalog(ttl=900)

        assert seen == ["https://zen.test/v1/models", MODELS_DEV_URL, OPENCODE_SNAPSHOT_URL]
        assert zen.catalog.ids() == ["a-free", "b-free"]
        assert zen.catalog.get("a-free").meta.context == 1048576

    @pytest.mark.asyncio
    async def test_zen_outage_keeps_serving_the_previous_catalog(self):
        state = {"fail": False}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "models.dev":
                return httpx.Response(200, json=PAYLOAD)
            if state["fail"]:
                return httpx.Response(503)
            return httpx.Response(200, json=ZEN_MODELS)

        zen = _client(handler)
        await zen.refresh_catalog(ttl=900)
        assert zen.catalog.ids() == ["a-free", "b-free"]

        state["fail"] = True
        zen._catalog_fetched_at = 0.0
        await zen.refresh_catalog(ttl=900)
        assert zen.catalog.ids() == ["a-free", "b-free"]

    @pytest.mark.asyncio
    async def test_both_sources_down_keeps_the_previous_catalog(self):
        state = {"fail": False}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "zen.test":
                return httpx.Response(200, json=ZEN_MODELS)
            if state["fail"]:
                raise httpx.ConnectError("down")
            return httpx.Response(200, json=PAYLOAD)

        zen = _client(handler)
        await zen.refresh_catalog(ttl=900)
        state["fail"] = True
        zen._catalog_fetched_at = 0.0
        await zen.refresh_catalog(ttl=900)
        assert zen.catalog.get("a-free").meta.context == 1048576

    @pytest.mark.asyncio
    async def test_failure_backs_off_instead_of_retrying_per_request(self):
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        zen = _client(handler)
        for _ in range(5):
            await zen.refresh_catalog(ttl=900)
        assert calls == 3  # zen, models.dev and its opencode copy, once

    @pytest.mark.asyncio
    async def test_source_without_our_provider_falls_through_to_the_mirror(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url == httpx.URL(MODELS_DEV_URL):
                return httpx.Response(200, json={"anthropic": {"models": {}}})
            if request.url == httpx.URL(OPENCODE_SNAPSHOT_URL):
                return httpx.Response(200, json=PAYLOAD)
            return httpx.Response(200, json=ZEN_MODELS)

        zen = _client(handler)
        await zen.refresh_catalog(ttl=900)
        assert zen.catalog.get("a-free").meta.context == 1048576

    @pytest.mark.asyncio
    async def test_oversized_payload_is_refused(self, monkeypatch):
        monkeypatch.setattr("zen_free_proxy.upstream.MAX_BYTES", 10)
        zen = _client(
            lambda request: httpx.Response(
                200, json=PAYLOAD if request.url.host == "models.dev" else ZEN_MODELS
            )
        )
        await zen.refresh_catalog(ttl=900)
        # Windows and routes come from the sources; without one only the names remain.
        assert zen.catalog.ids() == ["a-free", "b-free"]
        assert all(entry.meta is None for entry in zen.catalog)

    @pytest.mark.asyncio
    async def test_empty_zen_list_stops_serving_models_it_retired(self):
        state = {"ids": ZEN_MODELS}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "models.dev":
                return httpx.Response(200, json=PAYLOAD)
            return httpx.Response(200, json=state["ids"])

        zen = _client(handler)
        await zen.refresh_catalog(ttl=900)
        assert zen.catalog.ids()

        state["ids"] = {"object": "list", "data": [{"id": "b-free"}]}
        zen._catalog_fetched_at = 0.0
        zen._catalog_attempted_at = 0.0
        await zen.refresh_catalog(ttl=900)
        assert zen.catalog.ids() == ["b-free"]


class TestSettings:
    def test_defaults_point_at_models_dev_and_its_opencode_copy(self):
        settings = Settings()
        assert settings.models_dev_url == MODELS_DEV_URL
        assert settings.models_dev_mirror_url == OPENCODE_SNAPSHOT_URL
        assert settings.catalog_sources() == [MODELS_DEV_URL, OPENCODE_SNAPSHOT_URL]

    def test_blank_disables_a_source(self):
        assert Settings(models_dev_url="").models_dev_url is None
        assert Settings(models_dev_mirror_url="").catalog_sources() == [MODELS_DEV_URL]
