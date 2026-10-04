"""Translation between the Chat Completions API and the Responses API.

Zen serves most free models on ``/chat/completions`` and the rest on ``/responses``.
A client that only speaks chat completions (LiteLLM, Ollama, Continue, the plain
openai SDK, ...) still needs to reach those models, so this module converts in both
directions: request bodies, JSON responses, and streaming SSE frames.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from .sse import SSE_DONE, iter_sse_data, openai_error, sse_json

_CHAT_ROLE_TO_ITEM = {"user": "message", "assistant": "message", "tool": "function_call_output"}
_SSE_ROLE_EVENT = "response.created"


# --------------------------------------------------------------------------- request


def chat_request_to_responses(body: dict[str, Any]) -> dict[str, Any]:
    instructions: list[str] = []
    items: list[dict[str, Any]] = []

    for message in body.get("messages") or []:
        role = message.get("role")
        content = message.get("content")

        if role in ("system", "developer"):
            text = _as_text(content)
            if text:
                instructions.append(text)
            continue

        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id") or "",
                    "output": _as_text(content),
                }
            )
            continue

        if role == "assistant":
            text = _as_text(content)
            if text:
                items.append({"type": "message", "role": "assistant", "content": text})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                        "name": function.get("name") or "",
                        "arguments": function.get("arguments") or "{}",
                    }
                )
            continue

        if role == "user":
            items.append({"type": "message", "role": "user", "content": _content_parts(content)})

    out: dict[str, Any] = {
        "model": body.get("model"),
        "input": items,
        "stream": bool(body.get("stream")),
    }
    if instructions:
        out["instructions"] = "\n\n".join(instructions)
    for src, dst in (
        ("max_tokens", "max_output_tokens"),
        ("max_completion_tokens", "max_output_tokens"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
    ):
        if body.get(src) is not None:
            out[dst] = body[src]

    tools = body.get("tools")
    if tools:
        out["tools"] = [_flatten_tool(tool) for tool in tools]
    if body.get("tool_choice") is not None:
        out["tool_choice"] = body["tool_choice"]
    if body.get("parallel_tool_calls") is not None:
        out["parallel_tool_calls"] = body["parallel_tool_calls"]
    if body.get("response_format") is not None:
        out["text"] = {"format": body["response_format"]}
    if body.get("user"):
        out["user"] = body["user"]
    return out


def _flatten_tool(tool: dict[str, Any]) -> dict[str, Any]:
    if tool.get("type") != "function" or "function" not in tool:
        return tool
    function = tool["function"]
    return {
        "type": "function",
        "name": function.get("name"),
        "description": function.get("description") or "",
        "parameters": function.get("parameters") or {"type": "object", "properties": {}},
    }


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return "" if content is None else str(content)


def _content_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    parts: list[dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.append({"type": "input_text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            parts.append({"type": "input_image", "image_url": url})
    return parts or [{"type": "input_text", "text": ""}]


# -------------------------------------------------------------------------- response


def responses_to_chat_completion(response: dict[str, Any], model: str) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    text_parts.append(block.get("text") or "")
        elif item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {"name": item.get("name") or "", "arguments": item.get("arguments") or "{}"},
                }
            )

    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = message["content"] or None

    return {
        "id": _chat_id(response.get("id")),
        "object": "chat.completion",
        "created": response.get("created") or int(time.time()),
        "model": response.get("model") or model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": "tool_calls" if tool_calls else _status_finish(response.get("status")),
            }
        ],
        "usage": _usage(response.get("usage")),
    }


def _chat_id(responses_id: Any) -> str:
    if isinstance(responses_id, str) and responses_id:
        return responses_id.replace("resp_", "chatcmpl_", 1)
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _status_finish(status: Any) -> str:
    return "length" if status == "incomplete" else "stop"


def _usage(usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("input_tokens") or 0
    completion = usage.get("output_tokens") or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": usage.get("total_tokens") or prompt + completion,
    }


# ---------------------------------------------------------------------------- stream


async def responses_sse_to_chat_sse(
    source: AsyncIterator[bytes],
    model: str,
) -> AsyncIterator[str]:
    """Re-frame a Responses-API SSE stream as chat.completion.chunk frames."""
    chat_id: str | None = None
    created = int(time.time())
    text_started = False
    tool_index = 0
    finish_reason: str | None = None

    async for data in iter_sse_data(source):
        if data is None:
            continue
        if data == "[DONE]":
            break

        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue

        kind = event.get("type") or ""
        response = event.get("response") if isinstance(event.get("response"), dict) else {}

        if kind in ("response.created", "response.in_progress"):
            chat_id = chat_id or _chat_id(response.get("id"))
            yield sse_json(_chunk(chat_id, created, model, {"role": "assistant", "content": ""}, None))
            continue

        if kind == "response.output_text.delta":
            delta = event.get("delta") or ""
            if not delta:
                continue
            if not text_started:
                text_started = True
                yield sse_json(_chunk(chat_id, created, model, {"content": ""}, None))
            yield sse_json(_chunk(chat_id, created, model, {"content": delta}, None))
            continue

        if kind == "response.output_item.added" and event.get("item", {}).get("type") == "function_call":
            item = event["item"]
            finish_reason = "tool_calls"
            yield sse_json(
                _chunk(
                    chat_id,
                    created,
                    model,
                    {
                        "tool_calls": [
                            {
                                "index": tool_index,
                                "id": item.get("call_id") or item.get("id"),
                                "type": "function",
                                "function": {"name": item.get("name") or "", "arguments": ""},
                            }
                        ]
                    },
                    None,
                )
            )
            tool_index += 1
            continue

        if kind == "response.function_call_arguments.delta":
            yield sse_json(
                _chunk(
                    chat_id,
                    created,
                    model,
                    {
                        "tool_calls": [
                            {
                                "index": max(tool_index - 1, 0),
                                "function": {"arguments": event.get("delta") or ""},
                            }
                        ]
                    },
                    None,
                )
            )
            continue

        if kind == "response.completed":
            finish_reason = finish_reason or _status_finish(response.get("status"))
            yield sse_json(_chunk(chat_id, created, model, {}, finish_reason, response.get("usage")))
            yield SSE_DONE
            return

        if kind in ("response.failed", "response.incomplete", "error"):
            detail = event.get("error") or response.get("error") or {}
            message = detail.get("message") if isinstance(detail, dict) else None
            yield sse_json(
                _chunk(
                    chat_id,
                    created,
                    model,
                    {},
                    finish_reason or "stop",
                    None,
                    error=openai_error(message or "upstream responses stream failed", type_="upstream_error"),
                )
            )
            yield SSE_DONE
            return

    yield SSE_DONE


def _chunk(
    chat_id: str | None,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None,
    usage: Any = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": chat_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}],
    }
    if usage:
        payload["usage"] = _usage(usage)
    if error:
        payload["error"] = error["error"]
    return payload


__all__ = [
    "chat_request_to_responses",
    "responses_sse_to_chat_sse",
    "responses_to_chat_completion",
]
