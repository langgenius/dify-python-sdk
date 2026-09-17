"""Discovering the tools a Dify workspace can offer a workflow.

A tool node has to name a provider, a tool, and every parameter with the right
kind — and the identifiers come from the workspace, not from anything a caller
can reasonably type. So discovery is an online step: ask the workspace what it
has, then build workflows offline against what came back.

The parameter split is the part worth knowing. Dify marks each parameter with a
``form``:

* ``form`` — configured when the workflow is built, and stored in the node's
  ``tool_configurations``
* ``llm`` — supplied per run, and stored in ``tool_parameters``

Putting one in the other's place produces a node Dify accepts and then cannot
run, so :class:`ToolSpec` keeps the distinction and sorts arguments by it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolParameter:
    """One parameter of a tool, and where its value belongs."""

    name: str
    type: str = "string"
    required: bool = False
    #: ``"form"`` for build-time configuration, ``"llm"`` for per-run input.
    form: str = "form"
    default: Any = None
    label: str = ""

    @property
    def is_configuration(self) -> bool:
        """Whether this is set when the workflow is built."""
        return self.form == "form"


@dataclass(frozen=True)
class ToolSpec:
    """A tool as a workspace reports it, ready to become a node."""

    provider_id: str
    provider_name: str
    provider_type: str
    name: str
    label: str = ""
    parameters: Sequence[ToolParameter] = field(default_factory=tuple)
    plugin_unique_identifier: str | None = None

    def parameter(self, name: str) -> ToolParameter | None:
        return next((p for p in self.parameters if p.name == name), None)

    @property
    def configuration_names(self) -> list[str]:
        """Parameters set when the workflow is built."""
        return [p.name for p in self.parameters if p.is_configuration]

    @property
    def runtime_names(self) -> list[str]:
        """Parameters supplied per run."""
        return [p.name for p in self.parameters if not p.is_configuration]

    def __repr__(self) -> str:
        return f"ToolSpec({self.provider_name}/{self.name})"


@dataclass(frozen=True)
class ToolProvider:
    """A provider and the tools it offers."""

    id: str
    name: str
    type: str
    tools: Sequence[ToolSpec] = field(default_factory=tuple)
    plugin_unique_identifier: str | None = None
    author: str = ""
    authorized: bool = True

    def __getitem__(self, tool_name: str) -> ToolSpec:
        for tool in self.tools:
            if tool.name == tool_name:
                return tool
        known = ", ".join(t.name for t in self.tools) or "none"
        msg = f"{self.name!r} has no tool {tool_name!r}. It offers: {known}."
        raise KeyError(msg)

    def __iter__(self):
        return iter(self.tools)

    def __len__(self) -> int:
        return len(self.tools)

    def __repr__(self) -> str:
        return f"ToolProvider({self.name!r}, {len(self.tools)} tools)"


class ToolCatalog:
    """What a workspace can offer, indexed by provider name."""

    def __init__(self, providers: Sequence[ToolProvider]):
        self._providers = tuple(providers)

    def __getitem__(self, provider_name: str) -> ToolProvider:
        for provider in self._providers:
            if provider.name == provider_name or provider.id == provider_name:
                return provider
        known = ", ".join(p.name for p in self._providers) or "none"
        msg = (
            f"This workspace has no tool provider {provider_name!r}. "
            f"It has: {known}. Install the plugin in Dify first."
        )
        raise KeyError(msg)

    def __iter__(self):
        return iter(self._providers)

    def __len__(self) -> int:
        return len(self._providers)

    @property
    def providers(self) -> tuple[ToolProvider, ...]:
        return self._providers

    def find(self, tool_name: str) -> list[ToolSpec]:
        """Every tool with this name, across providers."""
        return [t for p in self._providers for t in p.tools if t.name == tool_name]

    def __repr__(self) -> str:
        names = ", ".join(p.name for p in self._providers)
        return f"ToolCatalog({names})"


def parse_provider(
    payload: Mapping[str, Any], tools: Sequence[Mapping[str, Any]]
) -> ToolProvider:
    """Build a provider from the console API's shapes."""
    provider_id = str(payload.get("id") or payload.get("name") or "")
    provider_name = str(payload.get("name") or provider_id)
    provider_type = str(payload.get("type") or "builtin")
    # Builtin providers report an empty identifier; only plugins carry a real one.
    plugin_id = payload.get("plugin_unique_identifier") or None

    specs = [
        ToolSpec(
            provider_id=provider_id,
            provider_name=provider_name,
            provider_type=provider_type,
            name=str(tool.get("name") or ""),
            label=_label(tool.get("label")) or str(tool.get("name") or ""),
            parameters=tuple(_parameter(p) for p in tool.get("parameters", [])),
            plugin_unique_identifier=plugin_id,
        )
        for tool in tools
    ]
    return ToolProvider(
        id=provider_id,
        name=provider_name,
        type=provider_type,
        tools=tuple(specs),
        plugin_unique_identifier=plugin_id,
        author=str(payload.get("author") or ""),
        authorized=bool(payload.get("is_team_authorization", True)),
    )


def _parameter(payload: Mapping[str, Any]) -> ToolParameter:
    return ToolParameter(
        name=str(payload.get("name") or ""),
        type=str(payload.get("type") or "string"),
        required=bool(payload.get("required")),
        form=str(payload.get("form") or "form"),
        default=payload.get("default"),
        label=_label(payload.get("label")),
    )


def _label(value: Any) -> str:
    """Pick a readable label out of Dify's i18n objects."""
    if isinstance(value, Mapping):
        for key in ("en_US", "en", "ja_JP", "zh_Hans"):
            if value.get(key):
                return str(value[key])
        return next((str(v) for v in value.values() if v), "")
    return str(value or "")
