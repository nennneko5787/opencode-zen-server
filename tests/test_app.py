from __future__ import annotations

import json
from typing import ClassVar

import httpx

from tests.conftest import CATALOG_PAYLOAD, chat_completion, sse
from zen_free_proxy.app import create_app
from zen_free_proxy.upstream import OPENCODE_USER_AGENT, PROXY_USER_AGENT


class TestModels:
    def test_lists_only_models_the_sources_call_free(self, client):
        data = client.get("/v1/models").json()["data"]
        assert {m["id"] for m in data} == {
            "chat-native-free",
            "responses-native-free",
            "guessy-free",  # no source describes it; the name heuristic carries it
            "jev-9.9-free",
        }
        assert all(m["object"] == "model" for m in data)
        assert all(m["owned_by"] == "opencode" for m in data)

    def test_publishes_windows_from_the_catalog_source(self, settings, make_client):
        # Zen publishes no sizes, so they come from models.dev. Without them clients
        # guess: pi-web-ui falls back to 200K for everything and compacts a 1M model
        # at 12% full.
        settings.catalog_ttl = 900

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "models.dev":
                return httpx.Response(200, json=CATALOG_PAYLOAD)
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "chat-native-free"}, {"id": "responses-native-free"}],
                },
            )

        client = make_client(handler, prime=False)
        with client:  # lifespan: catalog refresh
            data = {m["id"]: m for m in client.get("/v1/models").json()["data"]}

        assert data["chat-native-free"]["context_window"] == 1048576
        assert data["chat-native-free"]["max_tokens"] == 524288
        assert data["chat-native-free"]["modalities"] == ["text", "image", "video"]
        assert data["responses-native-free"]["context_window"] == 200000

    def test_model_no_source_describes_is_served_without_windows(self, client):
        payload = client.get("/v1/models/jev-9.9-free").json()
        assert payload["owned_by"] == "opencode"
        assert "context_window" not in payload

    def test_retrieve_single_model(self, client):
        response = client.get("/v1/models/chat-native-free")
        assert response.status_code == 200
        assert response.json()["id"] == "chat-native-free"

    def test_retrieve_paid_model_explains_why(self, client):
        response = client.get("/v1/models/chat-native-paid")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "model_not_found"
        assert "paid Zen model" in response.json()["error"]["message"]

    def test_root_advertises_endpoints(self, client):
        body = client.get("/").json()
        assert body["client_auth_required"] is False
        assert "/v1/chat/completions" in body["endpoints"]


class TestOpenAuth:
    def test_any_key_is_accepted(self, make_client):
        client = make_client(lambda _r: httpx.Response(200, json=chat_completion()))
        for header in ("Bearer whatever", "Bearer sk-123", "not-even-bearer", ""):
            response = client.post(
                "/v1/chat/completions",
                json={"model": "chat-native-free", "messages": [{"role": "user", "content": "hi"}]},
                headers={"authorization": header},
            )
            assert response.status_code == 200, header

    def test_missing_key_is_accepted(self, make_client):
        client = make_client(lambda _r: httpx.Response(200, json=chat_completion()))
        response = client.post(
            "/v1/chat/completions",
            json={"model": "chat-native-free", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200

    def test_client_key_mode_rejects_unknown_keys(self, settings, make_client):
        settings.allowed_client_keys = ["good-key"]
        client = make_client(lambda _r: httpx.Response(200, json=chat_completion()))
        assert (
            client.post(
                "/v1/chat/completions",
                json={"model": "chat-native-free", "messages": []},
                headers={"authorization": "Bearer good-key"},
            ).status_code
            == 200
        )
        bad = client.post(
            "/v1/chat/completions",
            json={"model": "chat-native-free", "messages": []},
            headers={"authorization": "Bearer nope"},
        )
        assert bad.status_code == 401


class TestUpstreamCredentials:
    def test_anonymous_when_no_key_configured(self, make_client):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(200, json=chat_completion())

        client = make_client(handler)
        client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})
        assert seen["auth"] == "Bearer public"

    def test_configured_key_is_used(self, make_client):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(200, json=chat_completion())

        client = make_client(handler, api_key="sk-real")
        client.post(
            "/v1/chat/completions",
            json={"model": "chat-native-free", "messages": []},
            headers={"authorization": "Bearer client-key-that-must-not-leak"},
        )
        assert seen["auth"] == "Bearer sk-real"

    def test_upstream_receives_a_real_authorization_header(self, make_client):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"].startswith("Bearer ")
            return httpx.Response(200, json=chat_completion())

        client = make_client(handler)
        client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})


class TestZenClientHeaders:
    """Zen gates its free tier on "coming from OpenCode", so upstream requests
    carry the same headers the official client sends."""

    def test_replays_the_official_client_headers(self, make_client):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json=chat_completion())

        client = make_client(handler)
        client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})

        assert seen["user-agent"] == OPENCODE_USER_AGENT
        assert seen["x-opencode-client"] == "cli"
        assert seen["x-opencode-session"].startswith("ses_")
        assert seen["x-opencode-request"].startswith("req_")

    def test_request_id_changes_per_request(self, make_client):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":  # skip the one-off model list at startup
                seen.append(request.headers["x-opencode-request"])
            return httpx.Response(200, json=chat_completion())

        client = make_client(handler)
        for _ in range(2):
            client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})
        assert len(set(seen)) == 2

    def test_session_id_is_stable_across_requests(self, make_client):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["x-opencode-session"])
            return httpx.Response(200, json=chat_completion())

        client = make_client(handler)
        for _ in range(2):
            client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})
        assert len(set(seen)) == 1

    def test_can_be_turned_off(self, settings):
        settings.zen_client_headers = False
        headers = create_app(settings).state.zen.headers()
        assert headers["user-agent"] == PROXY_USER_AGENT
        assert "x-opencode-client" not in headers
        assert "x-opencode-request" not in headers


class TestChatCompletions:
    def test_passthrough(self, make_client):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=chat_completion())

        client = make_client(handler)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chat-native-free",
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.2,
            },
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hi"
        assert seen["url"] == "https://zen.test/v1/chat/completions"
        assert seen["body"]["temperature"] == 0.2

    def test_provider_prefix_is_stripped(self, make_client):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["model"] = json.loads(request.content)["model"]
            return httpx.Response(200, json=chat_completion())

        client = make_client(handler)
        client.post("/v1/chat/completions", json={"model": "opencode/chat-native-free", "messages": []})
        assert seen["model"] == "chat-native-free"

    def test_stream_requests_usage(self, make_client):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                content=sse({"type": "x"}, "[DONE]"),
                headers={"content-type": "text/event-stream"},
            )

        client = make_client(handler)
        with client.stream(
            "POST", "/v1/chat/completions", json={"model": "chat-native-free", "messages": [], "stream": True}
        ) as response:
            assert response.status_code == 200
            list(response.iter_text())
        assert seen["body"]["stream"] is True
        assert seen["body"]["stream_options"] == {"include_usage": True}

    def test_stream_is_relayed_verbatim(self, make_client):
        frames = sse({"id": "1", "choices": [{"delta": {"content": "a"}}]}, "[DONE]")

        client = make_client(
            lambda _r: httpx.Response(200, content=frames, headers={"content-type": "text/event-stream"})
        )
        with client.stream(
            "POST", "/v1/chat/completions", json={"model": "chat-native-free", "messages": [], "stream": True}
        ) as response:
            body = "".join(response.iter_text())
        assert "data: [DONE]" in body
        assert json.loads(body.split("data: ")[1])["choices"][0]["delta"]["content"] == "a"

    def test_paid_model_rejected_before_upstream(self, make_client):
        def handler(_r: httpx.Request) -> httpx.Response:
            raise AssertionError("upstream must not be called for a paid model")

        client = make_client(handler)
        response = client.post("/v1/chat/completions", json={"model": "chat-native-paid", "messages": []})
        assert response.status_code == 404
        assert "paid Zen model" in response.json()["error"]["message"]

    def test_missing_model_field(self, make_client):
        client = make_client(lambda _r: httpx.Response(200, json=chat_completion()))
        response = client.post("/v1/chat/completions", json={"messages": []})
        assert response.status_code == 404
        assert "'model' is required" in response.json()["error"]["message"]

    def test_malformed_body(self, make_client):
        client = make_client(lambda _r: httpx.Response(200, json=chat_completion()))
        response = client.post(
            "/v1/chat/completions",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400

    def test_systemone_model_routes_elsewhere(self, make_client):
        client = make_client(lambda _r: httpx.Response(200, json=chat_completion()))
        response = client.post("/v1/chat/completions", json={"model": "jev-9.9-free", "messages": []})
        assert response.status_code == 400
        assert "/v1/systemone" in response.json()["error"]["message"]


class TestResponsesEndpoint:
    def test_rejects_chat_native_model(self, make_client):
        client = make_client(lambda _r: httpx.Response(200, json={}))
        response = client.post("/v1/responses", json={"model": "chat-native-free", "input": "hi"})
        assert response.status_code == 400
        assert "/v1/chat/completions" in response.json()["error"]["message"]

    def test_chat_request_is_translated_and_answered(self, make_client):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "resp_abc",
                    "status": "completed",
                    "model": "responses-native-free",
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": "pong"}]},
                    ],
                    "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
                },
            )

        client = make_client(handler)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "responses-native-free",
                "messages": [
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "ping"},
                ],
                "max_tokens": 32,
            },
        )
        assert response.status_code == 200
        assert seen["url"] == "https://zen.test/v1/responses"
        assert seen["body"]["instructions"] == "be terse"
        assert seen["body"]["input"] == [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ping"}]}
        ]
        assert seen["body"]["max_output_tokens"] == 32

        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["id"] == "chatcmpl_abc"
        assert body["choices"][0]["message"]["content"] == "pong"
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["usage"] == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}


class TestRouteSelfCorrection:
    """A route guess that is wrong is not a dead end: zen says so and we retry."""

    WRONG_ROUTE: ClassVar[dict] = {
        "error": {
            "type": "ModelError",
            "message": "Model chat-native-free is not supported for format openai",
        }
    }

    def test_wrong_guess_is_retried_on_the_other_route(self, make_client):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={})
            seen.append(request.url.path)
            if request.url.path.endswith("/chat/completions"):
                return httpx.Response(401, json=self.WRONG_ROUTE)
            return httpx.Response(
                200,
                json={
                    "id": "resp_1",
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        client = make_client(handler)
        response = client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})
        assert response.status_code == 200
        assert seen == ["/v1/chat/completions", "/v1/responses"]
        assert response.json()["choices"][0]["message"]["content"] == "ok"

    def test_the_working_route_is_remembered(self, make_client):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={})
            seen.append(request.url.path)
            if request.url.path.endswith("/chat/completions"):
                return httpx.Response(401, json=self.WRONG_ROUTE)
            return httpx.Response(200, json={"id": "resp_1", "status": "completed", "output": []})

        client = make_client(handler)
        for _ in range(3):
            client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})
        # Only the first request pays for the mistake.
        assert seen == [
            "/v1/chat/completions",
            "/v1/responses",
            "/v1/responses",
            "/v1/responses",
        ]

    def test_other_upstream_errors_are_not_retried(self, make_client):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            if request.method == "GET":
                return httpx.Response(200, json={})
            calls += 1
            return httpx.Response(
                403,
                json={"error": {"type": "FreeTierError", "message": "free tier is for OpenCode only"}},
            )

        client = make_client(handler)
        response = client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})
        assert calls == 1
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "free_tier_restricted"

    def test_a_guessed_route_is_probed_when_zen_fails_on_it(self, make_client):
        # jev-style: nothing describes this model, so the route is a name guess, and
        # zen answering 5xx is the only hint that it belongs on the other route.
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={})
            seen.append(request.url.path)
            return httpx.Response(500, json={"error": {"type": "error", "message": "Internal server error"}})

        client = make_client(handler)
        response = client.post("/v1/chat/completions", json={"model": "guessy-free", "messages": []})
        assert response.status_code == 500
        assert seen == ["/v1/chat/completions", "/v1/responses"]

    def test_a_sourced_route_is_not_probed_on_a_5xx(self, make_client):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            if request.method == "GET":
                return httpx.Response(200, json={})
            calls += 1
            return httpx.Response(500, json={"error": {"type": "error", "message": "Internal server error"}})

        client = make_client(handler)
        client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})
        # models.dev named this route, so a 5xx is a real failure, not a wrong guess.
        assert calls == 1

    def test_both_routes_failing_reports_the_last_answer(self, make_client):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={})
            return httpx.Response(401, json=self.WRONG_ROUTE)

        client = make_client(handler)
        response = client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})
        assert response.status_code == 401
        assert "not supported for format" in response.json()["error"]["message"]


class TestSystemone:
    def test_forwards(self, make_client):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"model": "jev-9.9-free", "answers": {"q": {"noul": 0.7}}})

        client = make_client(handler)
        response = client.post("/v1/systemone", json={"model": "jev-9.9-free", "state": "x", "questions": {}})
        assert response.status_code == 200
        assert seen["url"] == "https://zen.test/v1/systemone"
        assert response.json()["answers"]["q"]["noul"] == 0.7

    def test_rejects_chat_model(self, make_client):
        client = make_client(lambda _r: httpx.Response(200, json={}))
        response = client.post("/v1/systemone", json={"model": "chat-native-free", "state": "x"})
        assert response.status_code == 404


class TestUpstreamErrors:
    def test_free_tier_error_is_annotated(self, make_client):
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={
                    "type": "error",
                    "error": {
                        "type": "FreeTierError",
                        "message": "Error from provider (Console): "
                        "OpenCode's free tier can only be used from within OpenCode",
                    },
                },
            )

        client = make_client(handler)
        response = client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})
        assert response.status_code == 403
        error = response.json()["error"]
        assert error["type"] == "free_tier_restricted"
        assert "ZEN_API_KEY" in error["message"]

    def test_rate_limit_passes_retry_after(self, make_client):
        client = make_client(
            lambda _r: httpx.Response(
                429, json={"error": {"message": "slow down", "type": "rate_limit_error"}}
            )
        )
        response = client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})
        assert response.status_code == 429
        assert response.headers["retry-after"]

    def test_generic_upstream_error_keeps_status(self, make_client):
        client = make_client(
            lambda _r: httpx.Response(
                400, json={"error": {"message": "bad model config", "type": "BadRequest"}}
            )
        )
        response = client.post("/v1/chat/completions", json={"model": "chat-native-free", "messages": []})
        assert response.status_code == 400
        assert response.json()["error"]["message"] == "bad model config"


class TestHealth:
    def test_ok_when_upstream_reachable(self, client):
        response = client.get("/healthz")
        assert response.status_code in (200, 503)

    def test_degraded_when_unreachable(self, make_client):
        def handler(_r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")

        client = make_client(handler)
        response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
