"""Run a deployed workflow on a real Dify instance.

This is the billed path. Where ``StubLLM`` answers the model locally, this
executes the app in Dify itself — the same engine, plugins, credentials and
version that serve production traffic.

It streams, rather than using blocking mode, because the blocking response
reports only a token total: per-node inputs, outputs and cost arrive as node
events. Streaming is what lets a billed run be asserted on the same way as a
stubbed one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .._transport import Transport
from ..secrets import ApiKeyInput
from ..streams import collect_run
from .results import NodeResult, RunResult
from .usage import Usage

#: Event names the Dify stream uses for the pieces we care about.
_NODE_FINISHED = {"node_finished"}
_HUMAN_INPUT_REQUIRED = frozenset({"human_input_required"})
_WORKFLOW_FINISHED = {"workflow_finished"}
_TEXT_CHUNK = {"text_chunk", "message", "agent_message"}

#: App modes the Service API serves at /chat-messages rather than /workflows/run.
CHAT_MODES = frozenset({"advanced-chat", "chat", "agent-chat"})


def run_on_dify(
    inputs: Mapping[str, Any] | None = None,
    *,
    api_key: ApiKeyInput = None,
    base_url: str | None = None,
    mode: str = "workflow",
    query: str | None = None,
    conversation_id: str | None = None,
    user: str = "dify-python-sdk",
    timeout: float = 300.0,
) -> RunResult:
    """Run the app behind ``api_key`` on Dify and collect per-node results.

    ``api_key`` is the app's Service-API key (``app-…``); left out, it comes
    from ``DIFY_API_KEY`` like every other client in this SDK.

    ``mode`` decides the route, because the Service API serves the two kinds of
    app at different paths and rejects the wrong one outright: a ``workflow``
    app runs at ``/workflows/run``, while a chatflow — anything with an answer
    node — runs at ``/chat-messages`` and needs a ``query``.
    """
    if mode in CHAT_MODES:
        return _run_chat(
            inputs,
            api_key=api_key,
            base_url=base_url,
            query=query,
            conversation_id=conversation_id,
            user=user,
            timeout=timeout,
        )

    client = Transport(api_key=api_key, base_url=base_url, timeout=timeout)
    try:
        response = client._send_request(
            "POST",
            "/workflows/run",
            {
                "inputs": dict(inputs or {}),
                "response_mode": "streaming",
                "user": user,
            },
            stream=True,
        )
        return _collect(response.iter_lines())
    finally:
        client.close()


def _run_chat(
    inputs: Mapping[str, Any] | None,
    *,
    api_key: ApiKeyInput,
    base_url: str | None,
    query: str | None,
    conversation_id: str | None,
    user: str,
    timeout: float,
) -> RunResult:
    """Run a chatflow, whose turn is a message rather than a bare input set."""
    if not query:
        msg = (
            "A chatflow is driven by a message, so run_live() needs query=... — "
            "it becomes sys.query in the workflow. Only a `workflow` app (one "
            "that ends in an end node) runs on inputs alone."
        )
        raise ValueError(msg)

    client = Transport(api_key=api_key, base_url=base_url, timeout=timeout)
    try:
        body = {
            "inputs": dict(inputs or {}),
            "query": query,
            "user": user,
            "response_mode": "streaming",
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        response = client._send_request("POST", "/chat-messages", body, stream=True)
        return _collect(response.iter_lines())
    finally:
        client.close()


def _collect(lines) -> RunResult:
    """Kept as the name this module already used; the reading lives in streams."""
    return collect_run(lines)


def _iter_events(lines):
    """Yield parsed objects from a ``data: {...}`` server-sent event stream."""
    for raw in lines:
        line = raw.strip() if isinstance(raw, str) else raw.decode().strip()
        if not line or not line.startswith("data:"):
            continue
        body = line[len("data:") :].strip()
        if not body or body == "[DONE]":
            continue
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def _node_result(data: Mapping[str, Any]) -> NodeResult:
    return NodeResult(
        node_id=str(data.get("node_id") or ""),
        node_type=str(data.get("node_type") or ""),
        status=str(data.get("status") or ""),
        execution_id=str(data.get("id") or ""),
        index=int(data.get("index") or 0),
        title=str(data.get("title") or ""),
        inputs=dict(data.get("inputs") or {}),
        outputs=dict(data.get("outputs") or {}),
        process_data=dict(data.get("process_data") or {}),
        error=str(data.get("error") or ""),
        usage=_usage(data.get("execution_metadata"), data.get("elapsed_time")),
    )


def _usage(metadata: Mapping[str, Any] | None, elapsed: Any) -> Usage:
    """Kept as a name the module already used; the parsing lives on Usage."""
    return Usage.from_metadata(metadata, elapsed)
