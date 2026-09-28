"""The two agent nodes, and the arguments plugin-defined nodes take.

Dify has two: one driven by a *strategy* from a plugin, and one that runs an
Agent the workspace owns. Neither is in graphon — an agent is the server's
work — so their node data is restated here from Dify's own entities.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from graphon.entities.base_node_data import BaseNodeData
from graphon.enums import BuiltinNodeTypes
from pydantic import BaseModel, Field

from ._errors import NodeError

__all__ = [
    "AgentNodeData",
    "DifyAgentNodeData",
    "NodeInput",
    "declared_output",
    "packaged_data",
    "roster_data",
    "strategy_data",
]


class NodeInput(BaseModel):
    """One argument handed to a node that takes plugin-defined parameters.

    An agent strategy, a datasource and a plugin trigger all take the same
    shape, so they take the same type — the name says what it is rather than
    which node happened to want it first.
    """

    value: Any
    type: Literal["mixed", "variable", "constant"]


class AgentNodeData(BaseNodeData):
    """An agent node driven by a strategy from a plugin.

    The strategy names come from the installed plugin — ``console.plugins()``
    lists what a workspace has — and its parameters are whatever that strategy
    declares, which is why they are carried rather than typed.
    """

    type: str = BuiltinNodeTypes.AGENT
    agent_strategy_provider_name: str = ""
    agent_strategy_name: str = ""
    agent_strategy_label: str = ""
    agent_parameters: dict[str, NodeInput] = Field(default_factory=dict)
    tool_node_version: str | None = "2"


class DifyAgentNodeData(BaseNodeData):
    """An agent node running one of Dify's own Agents, rather than a plugin strategy.

    The node names an Agent through ``agent_binding``, and Dify turns that into
    a binding record while it imports the draft. Two ways to fill it, and they
    are not interchangeable:

    - ``{"binding_type": "roster_agent", "agent_id": …}`` points at a published
      Agent the workspace already has. The Agent is shared: other workflows may
      bind the same one, and publishing a new version of it changes them all.
    - ``{"binding_type": "inline_agent", "package_ref": …}`` ships the Agent
      *inside* the workflow, under the document's ``agent_packages``. Dify
      creates an Agent owned by this node on import, so the document is
      self-contained and nothing else can be changed by editing it.

    A binding with neither — the shape Dify's editor writes before an Agent is
    chosen — is skipped during import, and publishing then fails with
    "requires a binding before publishing".

    ``agent_task`` is what this node asks the Agent to do, and
    ``agent_declared_outputs`` the fields it must answer with. The inline route
    carries the same two inside ``agent_job``, because Dify reads a packaged
    node's job config from there.
    """

    type: str = BuiltinNodeTypes.AGENT
    version: str = "2"
    agent_node_kind: Literal["dify_agent"] = "dify_agent"
    agent_binding: dict[str, Any] = Field(default_factory=dict)
    agent_task: str = ""
    agent_declared_outputs: list[dict[str, Any]] = Field(default_factory=list)
    agent_job: dict[str, Any] | None = None


# -- building one ----------------------------------------------------------


def strategy_data(
    *,
    strategy: str,
    parameters: Mapping[str, NodeInput],
    label: str,
    title: str,
) -> AgentNodeData:
    """An agent node driven by a strategy from a plugin.

    ``strategy`` is ``provider/name`` as the plugin declares it. Dify resolves
    it when the node runs rather than when the workflow publishes, so a
    missing provider here is the last chance to catch a typo cheaply.
    """
    provider, _, name = strategy.rpartition("/")
    if not provider:
        msg = (
            f"strategy={strategy!r} is missing its provider. Use "
            "'provider/strategy', e.g. 'langgenius/agent/function_calling'."
        )
        raise NodeError(msg)
    return AgentNodeData(
        title=title,
        agent_strategy_provider_name=provider,
        agent_strategy_name=name,
        agent_strategy_label=label or name,
        agent_parameters=dict(parameters),
    )


def roster_data(
    *,
    agent_id: str,
    task: str,
    outputs: Sequence[Mapping[str, Any]],
    title: str,
) -> DifyAgentNodeData:
    """An agent node bound to a published Agent the workspace already has."""
    if not agent_id:
        msg = (
            "This node needs a roster Agent id, or an object carrying one. "
            "DifyManagement.agents.list() shows what the workspace has."
        )
        raise NodeError(msg)
    return DifyAgentNodeData(
        title=title,
        agent_binding={"binding_type": "roster_agent", "agent_id": agent_id},
        agent_task=task,
        agent_declared_outputs=[dict(output) for output in outputs],
    )


def packaged_data(
    *,
    package_ref: str,
    task: str,
    outputs: Sequence[Mapping[str, Any]],
    title: str,
) -> DifyAgentNodeData:
    """An agent node bound to an Agent shipped inside the same document.

    A packaged node's job config is read from ``agent_job`` rather than from
    the two loose keys a roster node uses, which is Dify's split and not one
    worth hiding.
    """
    return DifyAgentNodeData(
        title=title,
        agent_binding={"binding_type": "inline_agent", "package_ref": package_ref},
        agent_job={
            "workflow_prompt": task,
            "declared_outputs": [dict(output) for output in outputs],
        },
    )


def declared_output(
    name: str,
    type: str = "string",
    *,
    description: str = "",
    required: bool = True,
) -> dict[str, Any]:
    """One field an agent node must answer with, and what it is.

    Dify's own types: ``string``, ``number``, ``boolean``, ``object``,
    ``array``, ``file``. ``__reason`` and other reserved names are refused by
    the server rather than here, because the reserved set is its to change.
    """
    return {
        "name": name,
        "type": type,
        "description": description,
        "required": required,
    }
