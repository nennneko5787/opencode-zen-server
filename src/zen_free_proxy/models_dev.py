"""What Zen's models actually are, read from models.dev at runtime.

Zen's ``/models`` endpoint returns nothing but ids, so every question a client has
about a model has to be answered somewhere else. models.dev publishes the catalog
the official OpenCode client itself reads, and opencode commits a copy of that same
file to its repository and refreshes it daily
(``.github/workflows/models-snapshot.yml``), which is where the mirror below points.

That one payload answers all three questions this proxy has about a model:

* **is it free?** ``cost.input == 0`` — the same test opencode applies to decide
  which models an anonymous caller may see, so it cannot drift from Zen's own idea
  of the free tier the way a name heuristic does (``big-pickle`` is free but looks
  paid; ``qwen3.6-plus-free`` looks free and is).
* **which route serves it?** ``provider.npm``. ``@ai-sdk/openai`` means opencode
  talks to Zen over the Responses API, the provider default
  ``@ai-sdk/openai-compatible`` over chat/completions. Anthropic- and google-
  flavoured models come back unmapped; those are not free today, and the app's
  route fallback covers the day one is.
* **how big is it?** the ``limit`` block. A client that guesses is expensive:
  pi-web-ui defaults unknown models to 200K, so a 1M model gets compacted at 12%
  full.

Fetched lazily, cached for a day, and a failure is never fatal — the catalog falls
back to Zen's own list plus name heuristics and models are served without a context
window, which beats shipping a table that goes stale every time Zen launches a
model.
"""

from __future__ import annotations

import logging
from typing import Any

from .catalog import CatalogSource, ModelMeta, UpstreamFormat

log = logging.getLogger("zen_free_proxy.models_dev")

MODELS_DEV_URL = "https://models.dev/api.json"

#: opencode vendors the same payload and commits it once a day, so this mirror has
#: the same shape as :data:`MODELS_DEV_URL` and keeps the proxy serving free models
#: (with their windows) when models.dev itself is unreachable.
OPENCODE_SNAPSHOT_URL = (
    "https://raw.githubusercontent.com/anomalyco/opencode/v2/packages/core/src/models-dev/snapshot.txt"
)

#: Provider key inside api.json that describes OpenCode Zen.
PROVIDER = "opencode"

#: api.json is ~5MB. The cap only guards against a wrong URL eating all memory,
#: and matches the limit pi-web-ui applies to the same file.
MAX_BYTES = 25_000_000

#: Floor between attempts after a failure, so an unreachable models.dev cannot
#: turn every inference request into a 5MB download.
RETRY_FLOOR_SECONDS = 900.0

#: The AI SDK package opencode uses for a model, mapped to the Zen route that
#: package speaks. Anything else (anthropic, google, …) is left unresolved on
#: purpose: guessing a route we cannot serve is worse than the chat default.
_ROUTE_BY_NPM = {
    "@ai-sdk/openai": UpstreamFormat.RESPONSES,
    "@ai-sdk/openai-compatible": UpstreamFormat.CHAT,
}


def _positive(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def is_free_entry(raw: dict[str, Any]) -> bool:
    """models.dev's own definition of free: nothing in, nothing out.

    Mirrors opencode's anonymous-client filter (``cost.input === 0``) rather than
    its id spelling, so a free model with a paid-looking name is served and a paid
    model with a ``-free`` name is not.
    """
    cost = raw.get("cost")
    if not isinstance(cost, dict):
        return False
    price_in = _price(cost.get("input"))
    price_out = _price(cost.get("output"))
    if price_in is None:
        return False
    return price_in == 0 and (price_out is None or price_out == 0)


def has_price(raw: dict[str, Any]) -> bool:
    """True when the entry carries a price at all, as opposed to carrying none."""
    cost = raw.get("cost")
    return isinstance(cost, dict) and _price(cost.get("input")) is not None


def upstream_route(raw: dict[str, Any], provider: dict[str, Any]) -> UpstreamFormat | None:
    """Zen route for a model, from the SDK package opencode reaches it through."""
    override = raw.get("provider")
    npm = override.get("npm") if isinstance(override, dict) else None
    npm = npm or provider.get("npm")
    return _ROUTE_BY_NPM.get(npm) if isinstance(npm, str) else None


def parse_models_dev(payload: Any) -> dict[str, CatalogSource]:
    """Every model models.dev lists under the ``opencode`` provider, free or not.

    Paid models are kept as well: knowing a model exists and costs money is what
    lets ``/v1/models`` say "that one exists, no you cannot have it" instead of
    calling it unknown.
    """
    if not isinstance(payload, dict):
        return {}
    provider = payload.get(PROVIDER)
    models = provider.get("models") if isinstance(provider, dict) else None
    if not isinstance(models, dict):
        return {}
    provider = provider if isinstance(provider, dict) else {}

    out: dict[str, CatalogSource] = {}
    for key, raw in models.items():
        if not isinstance(raw, dict):
            continue
        out[key] = CatalogSource(
            id=key,
            free=is_free_entry(raw),
            priced=has_price(raw),
            route=upstream_route(raw, provider),
            meta=_meta(key, raw),
        )
    return out


def _meta(model_id: str, raw: dict[str, Any]) -> ModelMeta:
    limit = raw.get("limit")
    limit = limit if isinstance(limit, dict) else {}
    modalities = raw.get("modalities")
    inputs = modalities.get("input") if isinstance(modalities, dict) else None
    return ModelMeta(
        name=str(raw.get("name") or model_id),
        context=_positive(limit.get("context")),
        max_input=_positive(limit.get("input")),
        output=_positive(limit.get("output")),
        modalities=tuple(m for m in inputs if isinstance(m, str)) if isinstance(inputs, list) else (),
        reasoning=raw.get("reasoning") is True,
    )
