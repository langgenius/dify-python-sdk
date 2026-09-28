"""The tool node: a plugin's tool, and the two ways its parameters are given.

graphon owns the schema and runs the node. What is here is the part that only
a workspace can answer — which parameters a tool decides when the workflow is
built and which it takes per run — because putting one in the other's place
produces a node Dify accepts and then cannot run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from graphon.nodes.tool.entities import ToolNodeData

from ._errors import NodeError

__all__ = ["builtin_tool_data", "check_arguments", "tool_data"]


def check_arguments(
    spec: Any,
    config: Mapping[str, Any],
    params: Mapping[str, Any],
) -> None:
    """Reject names the tool does not have, or that belong on the other side."""
    known = {p.name: p for p in getattr(spec, "parameters", ())}
    if not known:
        return

    for group, names, expected in (
        ("config", config, True),
        ("params", params, False),
    ):
        for name in names:
            parameter = known.get(name)
            if parameter is None:
                available = ", ".join(sorted(known)) or "none"
                msg = f"{spec.name!r} has no parameter {name!r}. It takes: {available}."
                raise NodeError(msg)
            if parameter.is_configuration is not expected:
                other = "params" if expected else "config"
                kind = "supplied per run" if expected else "configured when built"
                msg = (
                    f"{name!r} is {kind}, so it belongs in {other}=, not "
                    f"{group}=. Dify accepts the node either way and then "
                    "cannot run it."
                )
                raise NodeError(msg)

    missing = [
        name
        for name, parameter in known.items()
        if parameter.required
        and parameter.is_configuration
        and name not in config
        and parameter.default is None
    ]
    if missing:
        msg = (
            f"{spec.name!r} needs {', '.join(sorted(missing))} in config=, "
            "and the tool declares no default."
        )
        raise NodeError(msg)


def tool_data(
    *,
    spec: Any,
    config: Mapping[str, Any],
    parameters: Mapping[str, Any],
    title: str | None,
) -> ToolNodeData:
    """One tool node, from a spec discovered on the workspace.

    ``parameters`` arrive already classified as Dify's constant / variable /
    mixed inputs; what this adds is the identity of the tool itself, which is
    the spec's to give.
    """
    check_arguments(spec, config, parameters)
    return ToolNodeData(
        title=title or getattr(spec, "label", None) or spec.name,
        provider_id=spec.provider_id,
        provider_type=spec.provider_type,
        provider_name=spec.provider_name,
        tool_name=spec.name,
        tool_label=getattr(spec, "label", None) or spec.name,
        tool_configurations=dict(config),
        tool_parameters=dict(parameters),
        plugin_unique_identifier=getattr(spec, "plugin_unique_identifier", None),
    )


def builtin_tool_data(
    *,
    provider: str,
    tool: str,
    label: str,
    parameters: Mapping[str, Any],
    title: str | None = None,
) -> ToolNodeData:
    """A tool node named outright rather than through a workspace catalogue.

    ``wf.tool(spec)`` is the way to add one, because the spec says which
    parameters are configured and which are supplied per run. This is for the
    few tools whose identifiers are fixed and published — Dify's own extractor
    and chunker, which every knowledge pipeline needs — so a workflow can name
    them without first reading a catalogue from a server.

    Dify resolves the plugin when the node runs, so a workspace that does not
    have it still imports and publishes the document; what fails is the run.
    """
    return ToolNodeData(
        title=title or label,
        provider_id=provider,
        provider_name=provider,
        provider_type="builtin",
        tool_name=tool,
        tool_label=label,
        tool_configurations={},
        tool_parameters=dict(parameters),
    )
