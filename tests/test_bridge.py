from __future__ import annotations

import json

import pytest

from zen_free_proxy.bridge import (
    chat_request_to_responses,
    responses_sse_to_chat_sse,
    responses_to_chat_completion,
)


async def collect(source) -> list[dict]:
    frames = [frame async for frame in source]
    payloads = []
    for frame in frames:
        for line in frame.strip().split("\n"):
            if line.startswith("data: ") and line != "data: [DONE]":
                payloads.append(json.loads(line.removeprefix("data: ")))
    return payloads


def sse_bytes(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class TestChatRequestToResponses:
    def test_system_message_becomes_instructions(self):
        out = chat_request_to_responses({"model": "m", "messages": [{"role": "system", "content": "s"}]})
        assert out["instructions"] == "s"
        assert out["input"] == []

    def test_plain_user_message(self):
        out = chat_request_to_responses({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        assert out["input"] == [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
        ]

    def test_tool_result_becomes_function_call_output(self):
        out = chat_request_to_responses(
            {"model": "m", "messages": [{"role": "tool", "tool_call_id": "call_1", "content": "42"}]}
        )
        assert out["input"] == [{"type": "function_call_output", "call_id": "call_1", "output": "42"}]

    def test_assistant_tool_calls_become_function_call_items(self):
        out = chat_request_to_responses(
            {
                "model": "m",
                "messages": [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                        ],
                    }
                ],
            }
        )
        assert out["input"][0]["type"] == "function_call"
        assert out["input"][0]["name"] == "f"

    def test_tools_are_flattened(self):
        out = chat_request_to_responses(
            {
                "model": "m",
                "messages": [],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "f", "description": "d", "parameters": {"type": "object"}},
                    }
                ],
            }
        )
        assert out["tools"] == [
            {"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}}
        ]

    def test_image_parts(self):
        out = chat_request_to_responses(
            {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                        ],
                    }
                ],
            }
        )
        assert out["input"][0]["content"][1] == {"type": "input_image", "image_url": "https://x/y.png"}

    def test_stream_flag_is_propagated(self):
        assert chat_request_to_responses({"model": "m", "messages": [], "stream": True})["stream"] is True


class TestResponsesToChatCompletion:
    def test_text(self):
        payload = responses_to_chat_completion(
            {
                "id": "resp_x",
                "status": "completed",
                "model": "m",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "a"},
                            {"type": "output_text", "text": "b"},
                        ],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
            "m",
        )
        assert payload["id"] == "chatcmpl_x"
        assert payload["choices"][0]["message"]["content"] == "ab"
        assert payload["choices"][0]["finish_reason"] == "stop"
        assert payload["usage"]["total_tokens"] == 3

    def test_function_call(self):
        payload = responses_to_chat_completion(
            {
                "status": "completed",
                "output": [
                    {"type": "function_call", "call_id": "call_1", "name": "f", "arguments": '{"a":1}'}
                ],
            },
            "m",
        )
        choice = payload["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["tool_calls"][0]["function"] == {"name": "f", "arguments": '{"a":1}'}

    def test_incomplete_status_maps_to_length(self):
        payload = responses_to_chat_completion({"status": "incomplete", "output": []}, "m")
        assert payload["choices"][0]["finish_reason"] == "length"


class TestResponsesStreamBridge:
    @pytest.mark.asyncio
    async def test_text_stream(self):
        frames = [
            {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
            {"type": "response.output_text.delta", "delta": "he"},
            {"type": "response.output_text.delta", "delta": "llo"},
            {
                "type": "response.completed",
                "response": {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
            },
        ]

        async def source():
            for block in sse_bytes(*frames).split(b"\n\n"):
                if block:
                    yield block + b"\n\n"

        out = "".join([chunk async for chunk in responses_sse_to_chat_sse(source(), "m")])
        assert "data: [DONE]" in out
        assert '"content":"he"' in out
        assert '"content":"llo"' in out
        assert '"finish_reason":"stop"' in out
        assert '"prompt_tokens":1' in out

    @pytest.mark.asyncio
    async def test_tool_call_stream(self):
        frames = [
            {"type": "response.created", "response": {"id": "resp_2"}},
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "call_id": "call_9", "name": "f"},
            },
            {"type": "response.function_call_arguments.delta", "delta": '{"a"'},
            {"type": "response.function_call_arguments.delta", "delta": ":1}"},
            {"type": "response.completed", "response": {"status": "completed"}},
        ]

        async def source():
            for block in sse_bytes(*frames).split(b"\n\n"):
                if block:
                    yield block + b"\n\n"

        out = "".join([chunk async for chunk in responses_sse_to_chat_sse(source(), "m")])
        payloads = [json.loads(frame.split("data: ", 1)[1]) for frame in out.strip().split("\n\n")[:-1]]
        arguments = "".join(
            call["function"].get("arguments", "")
            for payload in payloads
            for call in payload["choices"][0]["delta"].get("tool_calls") or []
        )
        assert '"finish_reason":"tool_calls"' in out
        assert '"call_9"' in out
        assert arguments == '{"a":1}'

    @pytest.mark.asyncio
    async def test_failure_event_ends_the_stream(self):
        frames = [
            {"type": "response.created", "response": {"id": "resp_3"}},
            {"type": "response.failed", "response": {"error": {"message": "boom"}}},
        ]

        async def source():
            for block in sse_bytes(*frames).split(b"\n\n"):
                if block:
                    yield block + b"\n\n"

        out = "".join([chunk async for chunk in responses_sse_to_chat_sse(source(), "m")])
        assert "boom" in out
        assert out.strip().endswith("data: [DONE]")

    @pytest.mark.asyncio
    async def test_done_sentinel_is_not_duplicated(self):
        completed = json.dumps({"type": "response.completed", "response": {"status": "completed"}})

        async def source():
            yield f"event: response.completed\ndata: {completed}\n\n".encode()
            yield b"data: [DONE]\n\n"

        out = "".join([chunk async for chunk in responses_sse_to_chat_sse(source(), "m")])
        assert out.count("[DONE]") == 1
