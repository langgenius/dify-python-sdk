"""Run a Dify DSL document locally with the graphon engine."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from graphon.dsl import loads
from graphon.dsl.errors import DslError
from graphon.engine_events.graph import (
    GraphRunAbortedEvent,
    GraphRunFailedEvent,
    GraphRunPartialSucceededEvent,
    GraphRunSucceededEvent,
)
from graphon.engine_events.node import (
    NodeRunFailedEvent,
    NodeRunStreamChunkEvent,
    NodeRunSucceededEvent,
)

from .results import NodeResult, RunResult, WorkflowRunError
from .usage import Usage


def run_dsl(
    dsl: str,
    *,
    inputs: Mapping[str, Any] | None = None,
    credentials: Mapping[str, Any] | str | None = None,
    workflow_id: str | None = None,
) -> RunResult:
    """Execute a DSL document and collect per-node results.

    The same document Dify would import is what runs here, so a passing local
    test is a statement about the workflow that gets deployed.
    """
    try:
        engine = loads(
            dsl,
            credentials=(
                dict(credentials) if isinstance(credentials, Mapping) else credentials
            ),
            workflow_id=workflow_id,
            start_inputs=dict(inputs or {}),
        )
    except DslError as exc:
        # Building the graph, not running it, is what failed: a node could not
        # be constructed at all. What to do about it depends on which piece was
        # missing, so the hint follows the error code rather than always
        # blaming credentials.
        raise WorkflowRunError(_build_failure_message(exc)) from exc

    nodes: dict[str, NodeResult] = {}
    stream: list[str] = []
    executions: list[NodeResult] = []
    status = "unknown"
    outputs: dict[str, Any] = {}
    error = ""

    # A failed graph both emits GraphRunFailedEvent and re-raises the node's
    # error out of the generator. Catch it so a failing run is an ordinary
    # RunResult a test can assert on, rather than an engine exception the
    # caller has to know about.
    try:
        for event in engine.run():
            if isinstance(event, (NodeRunSucceededEvent, NodeRunFailedEvent)):
                result = _node_result(event)
                # `nodes` keeps the last run of each node; `executions` keeps
                # every one, so a node inside a loop is counted each pass.
                nodes[event.node_id] = result
                executions.append(result)
            elif isinstance(event, NodeRunStreamChunkEvent):
                stream.append(event.chunk)
            elif isinstance(event, GraphRunSucceededEvent):
                status, outputs = "succeeded", dict(event.outputs)
            elif isinstance(event, GraphRunPartialSucceededEvent):
                status, outputs = "partial-succeeded", dict(event.outputs)
            elif isinstance(event, GraphRunFailedEvent):
                status, error = "failed", event.error
            elif isinstance(event, GraphRunAbortedEvent):
                status = "aborted"
    except Exception as exc:  # noqa: BLE001 - surfaced through RunResult.status
        status = "failed"
        error = error or str(exc)

    return RunResult(
        status=status,
        outputs=outputs,
        nodes=nodes,
        error=error,
        stream=stream,
        executions=executions,
    )


#: What to suggest for each family of DSL build failure.
_BUILD_HINTS: tuple[tuple[str, str], ...] = (
    (
        "credential.",
        "Pass credentials=..., or stub the model with "
        "dify_client.workflow.testing.StubLLM to run without one.",
    ),
    (
        "dependency.",
        'Declare the plugin with wf.depends_on("<plugin>:<version>@<hash>"), '
        "or stub the model with dify_client.workflow.testing.StubLLM.",
    ),
    (
        "runtime.slim",
        "A live run reaches the model through that binary. To test without "
        "one, stub the model with dify_client.workflow.testing.StubLLM.",
    ),
)


def _build_failure_message(exc: DslError) -> str:
    """Explain a build failure and name the fix that matches it."""
    detail = str(exc).rstrip(".")
    code = getattr(exc, "code", "") or ""
    for prefix, hint in _BUILD_HINTS:
        if code.startswith(prefix):
            return f"Could not build the workflow: {detail}. {hint}"
    return f"Could not build the workflow: {detail}."


def _node_result(event: NodeRunSucceededEvent | NodeRunFailedEvent) -> NodeResult:
    result = event.node_run_result
    return NodeResult(
        node_id=event.node_id,
        node_type=str(event.node_type),
        status=str(result.status),
        inputs=dict(result.inputs),
        outputs=dict(result.outputs),
        process_data=dict(result.process_data),
        error=getattr(event, "error", "") or result.error,
        usage=Usage.from_llm_usage(getattr(result, "llm_usage", None)),
    )
