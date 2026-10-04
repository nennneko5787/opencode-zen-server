"""Which Zen models this proxy is willing to expose, and on which upstream route.

No model is named anywhere in this file. The catalog is assembled from three live
sources, in order of trust:

1. ``GET {zen}/models`` — what the gateway serves *today*. This is the gate: a model
   Zen stops listing leaves ``/v1/models``, and one it launches shows up without a
   code change.
2. models.dev's ``opencode`` entry (``models_dev.py``) — the same catalog the
   official OpenCode client reads, which answers the two things an id cannot:
   free or not (``cost.input == 0``, the very test opencode applies when it hides
   paid models from anonymous clients) and which route serves the model
   (``provider.npm``: ``@ai-sdk/openai`` is the Responses API, the default
   ``@ai-sdk/openai-compatible`` is chat).
3. Name heuristics — only for models the two sources above have not caught up with
   (Zen ships a free model before models.dev lists it) and for ``jev-*``, which Zen
   serves on ``/systemone`` and models.dev does not carry at all.

Getting the route wrong is recoverable: Zen answers ``Model X is not supported for
format openai``, and the app retries the next route and remembers which one worked.
So a misread source heals itself at runtime instead of waiting for a patch.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace


class UpstreamFormat(enum.StrEnum):
    CHAT = "chat/completions"
    RESPONSES = "responses"
    SYSTEMONE = "systemone"


#: The two routes an OpenAI-shaped client can be proxied over, in the order they are
#: worth trying when the catalog's route guess is rejected by the gateway.
OPENAI_FORMATS: tuple[UpstreamFormat, UpstreamFormat] = (UpstreamFormat.CHAT, UpstreamFormat.RESPONSES)

#: Free tiers are not always suffixed with "-free": big-pickle is a stealth free
#: model and fledge ships as an alpha. Fallback only — models.dev decides whenever
#: it knows the model, so this never overrides real data.
_FREE_PATTERNS = (
    re.compile(r"-free$"),
    re.compile(r"-free-\d+$"),
    re.compile(r"^free-"),
    re.compile(r"^big-pickle$"),
    re.compile(r"^fledge-"),
)

#: Stripped from incoming model names so `opencode/space-bunny-free` works too.
_PROVIDER_PREFIXES = ("opencode/", "opencode-zen/", "zen/")

#: Zen serves the ``jev`` family from TypeSafe on its own route, and models.dev does
#: not list it at all, so the name is the only signal available.
_SYSTEMONE_PREFIX = "jev-"


def looks_free(model_id: str) -> bool:
    """Heuristic free detection, used only for models the sources do not describe."""
    return any(pattern.search(model_id) for pattern in _FREE_PATTERNS)


def normalize_model_id(model_id: str) -> str:
    name = model_id.strip()
    for prefix in _PROVIDER_PREFIXES:
        if name.lower().startswith(prefix):
            name = name[len(prefix) :]
            break
    return name


def default_route(model_id: str) -> UpstreamFormat:
    """Route for a model no source describes: chat, except for the ``jev`` family."""
    if model_id.startswith(_SYSTEMONE_PREFIX):
        return UpstreamFormat.SYSTEMONE
    return UpstreamFormat.CHAT


@dataclass(frozen=True, slots=True)
class ModelMeta:
    """A model's window and capabilities, in the shapes clients actually read."""

    name: str
    context: int | None = None
    max_input: int | None = None
    output: int | None = None
    modalities: tuple[str, ...] = ()
    reasoning: bool = False

    def to_openai(self) -> dict[str, object]:
        """Extra fields for a ``/v1/models`` entry.

        Each size is published under the spellings different clients look for:
        pi-web-ui reads ``context_window``/``max_tokens``/``modalities``, vLLM style
        endpoints use ``max_model_len``, OpenRouter uses ``context_length``, and
        models.dev consumers (opencode itself) read ``limit.context``.
        """
        body: dict[str, object] = {"name": self.name}
        if self.context:
            body["context_window"] = self.context
            body["context_length"] = self.context
            body["max_context_length"] = self.context
        if self.max_input:
            body["max_input_tokens"] = self.max_input
        if self.output:
            body["max_tokens"] = self.output
            body["max_output_tokens"] = self.output
        if self.modalities:
            body["modalities"] = list(self.modalities)
        if self.reasoning:
            body["reasoning"] = True
        if self.context or self.output:
            limit = {}
            if self.context:
                limit["context"] = self.context
            if self.max_input:
                limit["input"] = self.max_input
            if self.output:
                limit["output"] = self.output
            body["limit"] = limit
        return body


@dataclass(frozen=True, slots=True)
class CatalogSource:
    """What the upstream sources say about one model.

    ``free`` is a price, not a name: models.dev's ``cost.input == 0``, which is the
    test opencode itself applies to decide what an anonymous caller may see.
    ``priced`` separates "costs money" from "nobody said", which are different
    answers to give someone who asked for a paid model. ``route`` is ``None`` when
    the sources do not say (an anthropic- or google-flavoured model, or one models.dev
    has not listed yet), in which case the caller falls back to
    :func:`default_route`.
    """

    id: str
    free: bool = False
    priced: bool = False
    route: UpstreamFormat | None = None
    meta: ModelMeta | None = None


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    id: str
    upstream: UpstreamFormat
    created: int
    owned_by: str = "opencode"
    meta: ModelMeta | None = None

    def to_openai(self) -> dict[str, object]:
        body: dict[str, object] = {
            "id": self.id,
            "object": "model",
            "created": self.created,
            "owned_by": self.owned_by,
        }
        if self.meta is not None:
            body.update(self.meta.to_openai())
        return body

    @property
    def openai_compatible(self) -> bool:
        return self.upstream is not UpstreamFormat.SYSTEMONE


class Catalog:
    """Free models this proxy serves, rebuilt from Zen and models.dev."""

    def __init__(self, fallback_created: int = 0) -> None:
        self._fallback_created = fallback_created
        self._entries: dict[str, CatalogEntry] = {}
        self._upstream: set[str] = set()
        self._priced: set[str] = set()
        # Routes the gateway itself has corrected us on. They outrank the catalog
        # sources: this is the gateway's own answer, not a guess about it.
        self._learned: dict[str, UpstreamFormat] = {}
        # Models whose route came from a name pattern rather than a source. Only
        # these are worth probing on the other route, because only these are guesses.
        self._guessed: set[str] = set()

    def apply(self, upstream_ids: Iterable[str] | None, sources: Mapping[str, CatalogSource]) -> None:
        """Rebuild the free-model set from what Zen serves and what models.dev says.

        Nothing is seeded: an id that Zen does not list is never served, even if a
        source still describes it, so retired free models disappear on their own.
        """
        self._upstream = set(upstream_ids or ())
        entries: dict[str, CatalogEntry] = {}
        self._priced = {
            model_id
            for model_id, source in sources.items()
            if model_id in self._upstream and source.priced and not source.free
        }
        for model_id in sorted(self._upstream):
            source = sources.get(model_id)
            # Sources first, name second: a model models.dev prices is classified by
            # its price, and only an unlisted one falls back to the naming heuristic.
            if source is None:
                if not looks_free(model_id):
                    continue
            elif not source.free:
                continue
            described = source.route if source else None
            route = self._learned.get(model_id) or described or default_route(model_id)
            if model_id not in self._learned and described is None:
                self._guessed.add(model_id)
            meta = source.meta if source else None
            entries[model_id] = CatalogEntry(
                id=model_id, upstream=route, created=self._fallback_created, meta=meta
            )
        self._entries = entries
        self._guessed &= set(entries)

    def remember_route(self, model_id: str, route: UpstreamFormat) -> None:
        """Record the route the gateway accepted, after rejecting the catalog's."""
        entry = self._entries.get(model_id)
        if entry is None or entry.upstream is route:
            return
        self._learned[model_id] = route
        self._guessed.discard(model_id)
        self._entries[model_id] = replace(entry, upstream=route)

    def route_is_guessed(self, model_id: object) -> bool:
        """True when the route came from a name pattern rather than from a source."""
        return isinstance(model_id, str) and model_id in self._guessed

    def is_paid(self, model_id: object) -> bool:
        """True when a source priced this model above zero: it is a paid Zen model."""
        return isinstance(model_id, str) and model_id in self._priced

    def is_blocked(self, model_id: object) -> bool:
        """True when Zen serves the model but this proxy will not hand it out.

        Wider than :meth:`is_paid`: it also covers models no source prices, and ones
        the naming heuristic did not recognise as free.
        """
        return isinstance(model_id, str) and model_id in self._upstream and model_id not in self._entries

    def upstream_ids(self) -> list[str]:
        """Every model Zen lists, free or not."""
        return sorted(self._upstream)

    def __contains__(self, model_id: object) -> bool:
        return isinstance(model_id, str) and model_id in self._entries

    def __iter__(self):
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, model_id: str) -> CatalogEntry | None:
        return self._entries.get(model_id)

    def ids(self) -> list[str]:
        return sorted(self._entries)
