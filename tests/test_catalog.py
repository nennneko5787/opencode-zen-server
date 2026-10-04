import time

from zen_free_proxy.catalog import (
    OPENAI_FORMATS,
    Catalog,
    CatalogSource,
    ModelMeta,
    UpstreamFormat,
    default_route,
    looks_free,
    normalize_model_id,
)

CHAT_FREE = CatalogSource(id="a-free", free=True, priced=True, route=UpstreamFormat.CHAT)
RESPONSES_FREE = CatalogSource(
    id="b-free",
    free=True,
    priced=True,
    route=UpstreamFormat.RESPONSES,
    meta=ModelMeta(name="B", context=200000, output=32000),
)
PAID = CatalogSource(id="c-paid", free=False, priced=True, route=UpstreamFormat.CHAT)
UNPRICED = CatalogSource(id="d-unpriced")


class TestFreeHeuristic:
    """Only a fallback: models.dev prices decide whenever it knows the model."""

    def test_suffix_pattern(self):
        for name in ("nemotron-3.5-lightning-free", "deepseek-v4-flash-free", "x-free-2", "free-thing"):
            assert looks_free(name), name

    def test_stealth_free_models(self):
        assert looks_free("big-pickle")
        assert looks_free("fledge-alpha-free")

    def test_paid_models_do_not_look_free(self):
        for name in ("kimi-k3", "claude-opus-5-5", "gemini-3.8-flash", "gpt-6-astra"):
            assert not looks_free(name), name


class TestNormalize:
    def test_strips_provider_prefix(self):
        assert normalize_model_id("opencode/a-free") == "a-free"
        assert normalize_model_id("zen/b-free") == "b-free"

    def test_leaves_bare_id(self):
        assert normalize_model_id("a-free") == "a-free"


class TestDefaultRoute:
    def test_chat_unless_the_family_is_systemone(self):
        assert default_route("a-free") is UpstreamFormat.CHAT
        assert default_route("jev-1.13-free") is UpstreamFormat.SYSTEMONE


class TestCatalog:
    def test_nothing_is_seeded(self):
        # No model is named in the source, so an unrefreshed proxy serves nothing
        # rather than serving a list that went stale at release time.
        assert Catalog().ids() == []

    def test_only_free_models_are_served(self):
        catalog = Catalog(fallback_created=1000)
        catalog.apply(
            ["a-free", "b-free", "c-paid"],
            {"a-free": CHAT_FREE, "b-free": RESPONSES_FREE, "c-paid": PAID},
        )
        assert catalog.ids() == ["a-free", "b-free"]
        assert all(entry.created == 1000 for entry in catalog)

    def test_paid_models_are_known_but_not_served(self):
        catalog = Catalog()
        catalog.apply(
            ["a-free", "c-paid", "d-unpriced"],
            {"a-free": CHAT_FREE, "c-paid": PAID, "d-unpriced": UNPRICED},
        )
        assert catalog.ids() == ["a-free"]
        assert catalog.is_paid("c-paid")
        assert not catalog.is_paid("d-unpriced")  # nobody said it costs money
        assert catalog.is_blocked("d-unpriced")
        assert not catalog.is_paid("a-free")
        assert not catalog.is_paid("never-heard-of-it")
        assert catalog.upstream_ids() == ["a-free", "c-paid", "d-unpriced"]

    def test_pricing_beats_the_name(self):
        # A "-free" id that models.dev prices, and a paid-looking id that it does not.
        catalog = Catalog()
        catalog.apply(
            ["priced-free-looking", "stealth-free"],
            {"priced-free-looking": PAID},
        )
        assert catalog.ids() == ["stealth-free"]
        assert catalog.is_paid("priced-free-looking")

    def test_models_the_sources_have_not_caught_up_with_fall_back_to_names(self):
        catalog = Catalog()
        catalog.apply(["brand-new-flash-free", "brand-new-paid"], {})
        assert catalog.ids() == ["brand-new-flash-free"]
        assert catalog.get("brand-new-flash-free").upstream is UpstreamFormat.CHAT
        assert catalog.get("brand-new-flash-free").meta is None

    def test_systemone_family_defaults_to_its_own_route(self):
        catalog = Catalog()
        catalog.apply(["jev-9.9-free"], {})
        assert catalog.get("jev-9.9-free").upstream is UpstreamFormat.SYSTEMONE

    def test_retired_models_disappear(self):
        catalog = Catalog()
        catalog.apply(["a-free", "b-free"], {"a-free": CHAT_FREE, "b-free": RESPONSES_FREE})
        catalog.apply(["a-free"], {"a-free": CHAT_FREE, "b-free": RESPONSES_FREE})
        assert catalog.ids() == ["a-free"]
        assert catalog.is_paid("b-free") is False  # no longer listed at all, not paid

    def test_windows_and_route_come_from_the_source(self):
        catalog = Catalog()
        catalog.apply(["a-free", "b-free"], {"a-free": CHAT_FREE, "b-free": RESPONSES_FREE})
        assert catalog.get("b-free").upstream is UpstreamFormat.RESPONSES
        assert catalog.get("b-free").to_openai()["context_window"] == 200000
        assert catalog.get("a-free").to_openai() == {
            "id": "a-free",
            "object": "model",
            "created": catalog.get("a-free").created,
            "owned_by": "opencode",
        }

    def test_learned_route_overrides_the_catalog(self):
        catalog = Catalog()
        catalog.apply(["a-free"], {"a-free": CHAT_FREE})
        catalog.remember_route("a-free", UpstreamFormat.RESPONSES)
        assert catalog.get("a-free").upstream is UpstreamFormat.RESPONSES
        # ... including after a refresh that says otherwise: zen said so, not us.
        catalog.apply(["a-free"], {"a-free": CHAT_FREE})
        assert catalog.get("a-free").upstream is UpstreamFormat.RESPONSES

    def test_learned_route_ignores_models_that_are_not_served(self):
        catalog = Catalog()
        catalog.remember_route("c-paid", UpstreamFormat.RESPONSES)
        assert catalog.get("c-paid") is None

    def test_only_name_matched_routes_are_guesses(self):
        catalog = Catalog()
        catalog.apply(["a-free", "brand-new-free"], {"a-free": CHAT_FREE})
        assert catalog.route_is_guessed("a-free") is False  # a source named its route
        assert catalog.route_is_guessed("brand-new-free") is True

    def test_confirming_a_guess_stops_guessing(self):
        catalog = Catalog()
        catalog.apply(["brand-new-free"], {})
        catalog.remember_route("brand-new-free", UpstreamFormat.RESPONSES)
        assert catalog.route_is_guessed("brand-new-free") is False
        assert catalog.get("brand-new-free").upstream is UpstreamFormat.RESPONSES

    def test_openai_shape(self):
        catalog = Catalog(fallback_created=int(time.time()))
        catalog.apply(["a-free"], {"a-free": CHAT_FREE})
        payload = catalog.get("a-free").to_openai()
        assert payload["object"] == "model"
        assert payload["owned_by"] == "opencode"

    def test_systemone_model_is_not_openai_compatible(self):
        catalog = Catalog()
        catalog.apply(["a-free", "jev-1.13-free"], {"a-free": CHAT_FREE})
        assert catalog.get("jev-1.13-free").openai_compatible is False
        assert catalog.get("a-free").openai_compatible is True


def test_openai_formats_are_the_two_zen_routes_a_chat_client_can_use():
    assert set(OPENAI_FORMATS) == {UpstreamFormat.CHAT, UpstreamFormat.RESPONSES}
    assert UpstreamFormat.SYSTEMONE not in OPENAI_FORMATS
