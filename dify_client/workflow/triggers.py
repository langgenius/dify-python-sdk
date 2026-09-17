"""Trigger nodes: the ways a workflow starts without anyone pressing run.

A workflow normally begins at a start node and runs when something calls it.
A trigger node replaces that: Dify itself starts the run, on a schedule or when
a webhook is posted to.

These node types live in Dify's own ``core.workflow.nodes``, not in graphon, so
a workflow built around one can be written, exported and deployed from here but
**cannot be run locally** — ``wf.run()`` has no implementation to call. Test the
downstream nodes with a start node, then swap the trigger in for deployment, or
run the deployed app on Dify with ``wf.run_live()``.

A trigger only becomes real on publish: importing the DSL writes a draft, and
Dify materializes the trigger — and the webhook's URL — when the workflow is
published.
"""

from __future__ import annotations

from typing import Any, Literal, Sequence

from graphon.entities.base_node_data import BaseNodeData
from pydantic import BaseModel, Field

#: HTTP methods a webhook trigger may listen on, as Dify spells them.
WebhookMethod = Literal["get", "post", "head", "patch", "put", "delete"]

__all__ = [
    "ScheduleTriggerData",
    "WebhookMethod",
    "WebhookTriggerData",
    "WebhookParameter",
    "body_field",
    "header",
    "query_param",
]

#: What a webhook parameter may be typed as, per surface. Dify rejects the rest.
HEADER_TYPES = ("string",)
QUERY_TYPES = ("string", "number", "boolean")
BODY_TYPES = (
    "string",
    "number",
    "boolean",
    "object",
    "array[string]",
    "array[number]",
    "array[boolean]",
    "array[object]",
    "file",
)

FREQUENCIES = ("hourly", "daily", "weekly", "monthly")
WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


class WebhookParameter(BaseModel):
    """One field a webhook accepts, and the node output it becomes."""

    name: str
    type: str = "string"
    required: bool = False


def header(name: str, *, required: bool = False) -> WebhookParameter:
    """A header the webhook reads. Headers are strings and nothing else."""
    return WebhookParameter(name=name, type="string", required=required)


def query_param(
    name: str, type: str = "string", *, required: bool = False
) -> WebhookParameter:
    """A query-string field. One of string, number or boolean."""
    _check_type(name, type, QUERY_TYPES, "query parameter")
    return WebhookParameter(name=name, type=type, required=required)


def body_field(
    name: str, type: str = "string", *, required: bool = False
) -> WebhookParameter:
    """A body field. The widest set: objects, arrays and files included."""
    _check_type(name, type, BODY_TYPES, "body field")
    return WebhookParameter(name=name, type=type, required=required)


def _check_type(name: str, type: str, allowed: Sequence[str], what: str) -> None:
    if type not in allowed:
        msg = (
            f"A webhook {what} cannot be {type!r} ({name!r}). "
            f"Dify allows: {', '.join(allowed)}."
        )
        raise ValueError(msg)


class WebhookTriggerData(BaseNodeData):
    """A workflow that runs when its webhook URL is called.

    The declared headers, query parameters and body fields become this node's
    outputs, under their own names.
    """

    type: str = "trigger-webhook"
    method: WebhookMethod = "post"
    content_type: str = "application/json"
    headers: list[WebhookParameter] = Field(default_factory=list)
    params: list[WebhookParameter] = Field(default_factory=list)
    body: list[WebhookParameter] = Field(default_factory=list)
    status_code: int = 200
    response_body: str = ""


class ScheduleTriggerData(BaseNodeData):
    """A workflow that runs on a clock.

    Two modes. ``cron`` takes an expression outright. ``visual`` takes a
    frequency plus a ``visual_config`` saying when within it — which is what
    the Dify editor writes, so it is what the editor can show back.
    """

    type: str = "trigger-schedule"
    mode: Literal["visual", "cron"] = "visual"
    frequency: str | None = None
    cron_expression: str | None = None
    visual_config: dict[str, Any] | None = None
    timezone: str = "UTC"
