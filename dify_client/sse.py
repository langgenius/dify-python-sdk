"""Decoding one server-sent event stream. Internal.

Dify streams as SSE: ``data: {…}`` lines, blank lines between, and periodic
keepalives. Nothing here knows what a run is — that is :mod:`dify_client.streams`,
which turns these payloads into events about one.

Kept apart because the two answer different questions, and an earlier version
that mixed them ended up with two event types for the same thing.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["TEXT_FIELDS", "decode"]

#: Events that carry a piece of the answer, and the field holding it.
#:
#: A chatflow streams ``message`` with the text at the top level; a workflow app
#: streams ``text_chunk`` with it one level down in ``data``. Getting that wrong
#: yields silence rather than an error, which is why it is written down once.
TEXT_FIELDS: dict[str, str] = {
    "message": "answer",
    "agent_message": "answer",
    "text_chunk": "text",
}

#: Sent to hold the connection open. Never interesting.
KEEPALIVE = "ping"


def decode(line: str) -> dict[str, Any] | None:
    """One SSE line as an event payload, or None if there is nothing in it.

    Returns None for blanks, comments, keepalives, and anything that is not a
    JSON object — a malformed line ends that line, not the stream.
    """
    if not line or not line.startswith("data:"):
        return None
    body = line[len("data:") :].strip()
    if not body:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("event") == KEEPALIVE:
        return None
    return payload


def text_of(payload: dict[str, Any]) -> str:
    """The piece of the answer this payload carries, if it carries one."""
    field = TEXT_FIELDS.get(str(payload.get("event", "")))
    if field is None:
        return ""
    value = payload.get(field)
    if value is None:
        nested = payload.get("data")
        value = nested.get(field) if isinstance(nested, dict) else None
    return str(value) if value is not None else ""
