"""Minimal Server-Sent Events framing: enough to relay and re-frame LLM streams."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

SSE_DONE = "data: [DONE]\n\n"


def sse_json(payload: Any, event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def iter_sse_data(source: AsyncIterator[bytes]) -> AsyncIterator[str | None]:
    """Yield the ``data:`` payload of each SSE frame.

    Yields ``None`` for frames without a data line (comments, bare ``event:`` lines)
    so callers can tell them apart from the ``[DONE]`` sentinel.
    """
    buffer = ""
    async for raw in source:
        buffer += raw.decode("utf-8", errors="replace")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            yield _frame_data(frame)
    if buffer.strip():
        tail = _frame_data(buffer)
        if tail is not None:
            yield tail


def _frame_data(frame: str) -> str | None:
    lines = [line[len("data:") :].lstrip() for line in frame.split("\n") if line.startswith("data:")]
    return "\n".join(lines) if lines else None


def openai_error(
    message: str, *, type_: str = "invalid_request_error", code: str | None = None
) -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "param": None, "code": code}}
