from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from zen_free_proxy.app import create_app
from zen_free_proxy.config import Settings
from zen_free_proxy.models_dev import parse_models_dev
from zen_free_proxy.upstream import ZenClient

Handler = Callable[[httpx.Request], httpx.Response]

#: A models.dev payload shaped like the real one, trimmed to a few models. Nothing
#: in the proxy knows any of these ids: what is free and which route serves it comes
#: from ``cost`` and ``provider.npm`` exactly as it does against the live file.
CATALOG_PAYLOAD = {
    "opencode": {
        "id": "opencode",
        "npm": "@ai-sdk/openai-compatible",
        "models": {
            "chat-native-free": {
                "id": "chat-native-free",
                "name": "Chat Native Free",
                "reasoning": True,
                "cost": {"input": 0, "output": 0},
                "limit": {"context": 1048576, "input": 524288, "output": 524288},
                "modalities": {"input": ["text", "image", "video"], "output": ["text"]},
            },
            "responses-native-free": {
                "id": "responses-native-free",
                "name": "Responses Native Free",
                "cost": {"input": 0, "output": 0},
                "limit": {"context": 200000, "output": 32000},
                "modalities": {"input": ["text"], "output": ["text"]},
                "provider": {"npm": "@ai-sdk/openai"},
            },
            "chat-native-paid": {
                "id": "chat-native-paid",
                "name": "Chat Native Paid",
                "cost": {"input": 1.5, "output": 6},
                "limit": {"context": 200000, "output": 32000},
            },
            "priced-free-looking": {
                "id": "priced-free-looking",
                "cost": {"input": 2, "output": 8},
                "limit": {"context": 200000, "output": 32000},
            },
        },
    }
}

#: What Zen's ``/models`` returns: free models, paid models, one no source prices,
#: and one Zen has not told models.dev about yet (the systemone family, and the only
#: kind of model whose route is a name-based guess).
ZEN_IDS = [
    "chat-native-free",
    "responses-native-free",
    "chat-native-paid",
    "priced-free-looking",
    "mystery-model",
    "guessy-free",
    "jev-9.9-free",
]

SOURCES = parse_models_dev(CATALOG_PAYLOAD)


def sse(*events: dict | str) -> bytes:
    body = ""
    for event in events:
        if isinstance(event, str):
            body += f"data: {event}\n\n"
        elif event.get("type"):
            body += f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        else:
            body += f"data: {json.dumps(event)}\n\n"
    return body.encode()


@pytest.fixture
def settings() -> Settings:
    return Settings(catalog_ttl=0, request_timeout=5, connect_timeout=5, max_retries=0)


@pytest.fixture
def make_client(settings: Settings):
    def _make(handler: Handler, api_key: str | None = None, *, prime: bool = True) -> TestClient:
        transport = httpx.MockTransport(handler)
        zen = ZenClient(
            "https://zen.test/v1",
            api_key,
            timeout=settings.request_timeout,
            connect_timeout=settings.connect_timeout,
            max_retries=settings.max_retries,
            transport=transport,
        )
        if prime:
            # Same code path the background refresh uses, minus the I/O: what is free
            # and which route serves it is read out of the payload, not a fixture.
            zen.apply_catalog(ZEN_IDS, SOURCES)
        return TestClient(create_app(settings, zen))

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client(lambda _request: httpx.Response(200, json={}))


def chat_completion(**overrides) -> dict:
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "chat-native-free",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    body.update(overrides)
    return body
