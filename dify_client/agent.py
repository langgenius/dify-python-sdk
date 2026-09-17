"""Dify Agents as code.

An Agent's configuration — its *soul* — is defined by pydantic models that live
inside the Dify server and are not published as a package. Workflow nodes are
different: their schemas come from ``graphon``, so this SDK can validate a
workflow against the same definitions the server uses. Nothing equivalent
exists for Agents yet.

Rather than copy a thousand lines of schema here and watch it drift, this
module treats the soul as data it carries but does not interpret. What it does
enforce is the part that does not need the schema: the envelope Dify expects,
and the removal of credentials before anything is written to a file.

That makes the practical route **export first**::

    console = DifyManagement()
    agent = Agent.from_yaml(console.export_app(APP_ID))
    agent.soul["model"]["completion_params"]["temperature"] = 0.2
    agent.to_yaml("support-triage.yml")       # commit this
    console.provision(agent)                  # and deploy it back

Configure an Agent in the Dify UI once, export it, and from then on the file in
your repository is the source of truth.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: The DSL version this module emits, matching the workflow builder.
DSL_VERSION = "0.7.0"

#: The schema version of an agent package, as Dify's AgentPackage declares it.
PACKAGE_SCHEMA_VERSION = 1

#: The key an agent app's DSL uses to point at its package.
PACKAGE_REF = "agent_1"

#: Key fragments whose values are stripped before export, mirroring what Dify's
#: own portable-package builder removes.
_SENSITIVE_FRAGMENTS = ("credential", "secret", "password")
_SENSITIVE_KEYS = {
    "api_key",
    "token",
    "access_token",
    "refresh_token",
    "upload_file_id",
}


class AgentError(Exception):
    """Raised when an agent document is not one Dify would accept."""


def default_soul() -> dict[str, Any]:
    """The soul Dify writes for a newly created Agent.

    Copied from what a real 1.17 instance produced for a fresh Agent, rather
    than invented: every section present, every default in place. Building on
    it means a hand-made Agent has the shape Dify's importer expects, which
    matters because this SDK cannot validate the soul — those models live in
    the server and are not published.
    """
    file_upload = {
        "allowed_file_extensions": ["JPG", "JPEG", "PNG", "GIF", "WEBP", "SVG"],
        "allowed_file_types": ["document", "image", "audio", "video"],
        "allowed_file_upload_methods": ["local_file", "remote_url"],
        "enabled": True,
        "image": {"enabled": True},
        "number_limits": 3,
    }
    features = {
        "file_upload": file_upload,
        "opening_statement": None,
        "retriever_resource": None,
        "sensitive_word_avoidance": None,
        "speech_to_text": None,
        "suggested_questions": None,
        "suggested_questions_after_answer": None,
        "text_to_speech": None,
    }
    return {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "prompt": {"system_prompt": ""},
        "model": None,
        "tools": {"cli_tools": [], "dify_tools": []},
        "knowledge": {"sets": []},
        "human": {"contacts": [], "tools": []},
        "env": {"secret_refs": [], "variables": []},
        "memory": {"artifacts": [], "budget": None, "scope": None},
        "sandbox": {
            "config": {"cpu": None, "env": [], "image": None, "working_dir": None},
            "provider": None,
        },
        "config_files": [],
        "config_skills": [],
        "config_note": "",
        "app_variables": [],
        "app_features": copy.deepcopy(features),
        "misc_legacy": copy.deepcopy(features),
    }


def model_config(
    model: str,
    *,
    plugin_id: str | None = None,
    **settings: Any,
) -> dict[str, Any]:
    """Build a soul's ``model`` section from a Dify model reference.

    ``model`` is ``provider/plugin/name:model``, the same spelling workflows
    use. ``plugin_id`` defaults to the first two segments of the provider,
    which is how Dify's own agents record it.
    """
    provider, _, name = model.rpartition(":")
    if not provider or not name:
        msg = (
            f"model={model!r} is not a Dify model reference. Use "
            "'provider/plugin/name:model', e.g. "
            "'langgenius/openai/openai:gpt-4o-mini'."
        )
        raise AgentError(msg)
    section: dict[str, Any] = {
        "plugin_id": plugin_id or "/".join(provider.split("/")[:2]),
        "model_provider": provider,
        "model": name,
        "credential_ref": None,
    }
    if settings:
        section["model_settings"] = {k: v for k, v in settings.items() if v is not None}
    return section


def dify_tool(
    spec: Any,
    *,
    enabled: bool = True,
    description: str | None = None,
    **runtime_parameters: Any,
) -> dict[str, Any]:
    """An entry for a soul's ``tools.dify_tools``, from a discovered tool spec.

    ``spec`` is what ``DifyManagement.tools.catalog()`` returns, so the identifiers come
    from the workspace rather than from guesswork.
    """
    return {
        "enabled": enabled,
        "provider_type": getattr(spec, "provider_type", "builtin"),
        "provider_id": getattr(spec, "provider_id", None),
        "plugin_id": _plugin_id_of(spec),
        "provider": getattr(spec, "provider_name", None),
        "tool_name": spec.name,
        "name": spec.name,
        "description": description or getattr(spec, "label", "") or spec.name,
        "credential_type": "unauthorized",
        "credential_ref": None,
        "runtime_parameters": dict(runtime_parameters),
    }


def _plugin_id_of(spec: Any) -> str | None:
    identifier = getattr(spec, "plugin_unique_identifier", None)
    if not identifier:
        return None
    return str(identifier).split(":", 1)[0]


#: Fields of a secret reference that name it without revealing it.
_SAFE_REF_KEYS = ("name", "key", "env_name", "variable", "type", "provider")


def strip_sensitive(value: Any) -> Any:
    """Blank credential-shaped entries, by key name.

    This is the blanket sweep Dify applies to a tool's ``runtime_parameters``,
    where the keys are whatever a tool author chose and no schema constrains
    them. It is deliberately **not** applied to a whole soul: doing that nulls
    fields that merely read as secret — ``credential_type`` is an enum and
    ``secret_refs`` is a list — and produces a document Dify refuses to import.
    """
    if isinstance(value, list):
        return [strip_sensitive(item) for item in value]
    if not isinstance(value, dict):
        return value

    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        lowered = key.lower()
        sensitive = (
            any(fragment in lowered for fragment in _SENSITIVE_FRAGMENTS)
            or lowered in _SENSITIVE_KEYS
            or lowered.endswith("file_id")
        )
        cleaned[key] = None if sensitive else strip_sensitive(item)
    return cleaned


def _safe_refs(refs: Any) -> list[dict[str, Any]]:
    """Keep the naming half of each secret reference, drop the rest."""
    if not isinstance(refs, list):
        return []
    return [
        {key: ref.get(key) for key in _SAFE_REF_KEYS if ref.get(key) is not None}
        for ref in refs
        if isinstance(ref, dict)
    ]


def portable_soul(soul: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``soul`` with credentials removed, the way Dify's exporter does.

    Ported field by field from Dify's ``make_portable_agent_package`` rather
    than approximated: each credential is handled where it lives, so the
    structure the importer validates against survives. A blanket sweep does not
    — it turns ``credential_type`` into null and Dify rejects the document.
    """
    data = copy.deepcopy(dict(soul))

    for section in ("config_skills", "config_files"):
        for item in data.get(section) or []:
            if isinstance(item, dict):
                item["file_id"] = ""
                item["is_missing"] = True

    model = data.get("model")
    if isinstance(model, dict):
        model["credential_ref"] = None

    tools = data.get("tools") or {}
    for tool in tools.get("dify_tools") or []:
        if not isinstance(tool, dict):
            continue
        tool["credential_type"] = "unauthorized"
        tool["credential_ref"] = None
        tool["runtime_parameters"] = strip_sensitive(
            tool.get("runtime_parameters") or {}
        )

    for tool in tools.get("cli_tools") or []:
        if not isinstance(tool, dict):
            continue
        env = tool.get("env") or {}
        env["secret_refs"] = _safe_refs(env.get("secret_refs"))
        tool["env"] = env

    env = data.setdefault("env", {})
    if isinstance(env, dict):
        env["secret_refs"] = _safe_refs(env.get("secret_refs"))

    for contact in (data.get("human") or {}).get("contacts") or []:
        if isinstance(contact, dict):
            for key in ("id", "contact_id", "human_id", "tenant_id"):
                contact[key] = None

    return data


@dataclass
class Agent:
    """A Dify Agent held as code.

    ``soul`` is the Agent's configuration, carried as plain data. This SDK does
    not validate it — the schema is not published — so build one by exporting
    an Agent you configured in Dify, then editing it here.
    """

    name: str
    soul: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    role: str = ""
    icon: str | None = "\U0001f916"
    icon_type: str | None = "emoji"
    icon_background: str | None = "#FFEAD5"
    workspace_skills: list[dict[str, Any]] = field(default_factory=list)
    omitted_assets: list[dict[str, Any]] = field(default_factory=list)

    #: Agent apps are always this mode.
    mode = "agent"

    # -- building ----------------------------------------------------------

    @classmethod
    def create(
        cls,
        name: str,
        *,
        instruction: str = "",
        model: str | None = None,
        tools: Sequence[Mapping[str, Any]] = (),
        skills: Sequence[str] = (),
        role: str = "",
        description: str = "",
        icon: str | None = "\U0001f916",
        icon_background: str | None = "#FFEAD5",
        **model_settings: Any,
    ) -> Agent:
        """Build an Agent from scratch on Dify's own default soul.

        Covers what most Agents need — an instruction, a model, some tools —
        and leaves ``agent.soul`` a plain dict for everything else::

            agent = Agent.create(
                "support-triage",
                instruction="Decide whether an incoming message needs paging.",
                model="langgenius/openai/openai:gpt-4o-mini",
                tools=[dify_tool(catalog["time"]["current_time"])],
                temperature=0.2,
            )
            agent.soul["memory"]["scope"] = "..."      # anything not covered

        This SDK cannot check the soul — those models live inside Dify — so
        what it does instead is start from the shape Dify itself writes.
        """
        soul = default_soul()
        soul["prompt"]["system_prompt"] = instruction
        if model:
            soul["model"] = model_config(model, **model_settings)
        elif model_settings:
            msg = "Model settings were given without a model to apply them to."
            raise AgentError(msg)
        if tools:
            soul["tools"]["dify_tools"] = [dict(t) for t in tools]

        agent = cls(
            name=name,
            soul=soul,
            description=description,
            role=role,
            icon=icon,
            icon_background=icon_background,
        )
        for skill in skills:
            agent.use_skill(skill)
        return agent

    @property
    def instruction(self) -> str:
        """The agent's system prompt."""
        return str((self.soul.get("prompt") or {}).get("system_prompt", ""))

    @instruction.setter
    def instruction(self, value: str) -> None:
        self.soul.setdefault("prompt", {})["system_prompt"] = value

    @property
    def model(self) -> str | None:
        """The agent's model, as ``provider:model``, or None if unset."""
        section = self.soul.get("model")
        if not isinstance(section, Mapping):
            return None
        return f"{section.get('model_provider')}:{section.get('model')}"

    def use_model(self, model: str, **settings: Any) -> None:
        """Point the agent at a model."""
        self.soul["model"] = model_config(model, **settings)

    def use_skill(
        self,
        name: str,
        *,
        priority: int | None = None,
        display_name: str = "",
        description: str = "",
    ) -> None:
        """Bind a workspace skill to this Agent, by name.

        The skill itself lives in the workspace and must be imported and
        published there first; an Agent only records the binding. ``priority``
        orders the skills an Agent has, lowest first, and defaults to the next
        free slot.
        """
        if priority is None:
            priority = len(self.workspace_skills)
        self.workspace_skills = [
            s for s in self.workspace_skills if s.get("name") != name
        ] + [
            {
                "name": name,
                "display_name": display_name,
                "description": description,
                "priority": priority,
            }
        ]

    @property
    def skills(self) -> list[str]:
        """Names of the workspace skills bound to this Agent."""
        return [str(s.get("name", "")) for s in self.workspace_skills]

    def add_tool(self, spec: Any, **runtime_parameters: Any) -> None:
        """Give the agent a tool discovered on the workspace."""
        tools = self.soul.setdefault("tools", {}).setdefault("dify_tools", [])
        tools.append(dify_tool(spec, **runtime_parameters))

    # -- reading -----------------------------------------------------------

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> Agent:
        """Read an Agent out of an exported app DSL document."""
        app = document.get("app")
        if not isinstance(app, dict):
            msg = "This document has no 'app' section, so it is not a Dify DSL export."
            raise AgentError(msg)
        if app.get("mode") != cls.mode:
            msg = (
                f"This DSL is a {app.get('mode')!r} app, not an agent. "
                "Use dify_client.workflow.Workflow for workflow and chatflow apps."
            )
            raise AgentError(msg)

        packages = document.get("agent_packages")
        reference = (document.get("agent") or {}).get("package_ref")
        if not isinstance(packages, dict) or reference not in packages:
            msg = (
                "This agent DSL has no package to read: expected an "
                "'agent_packages' mapping and an 'agent.package_ref' naming one of them."
            )
            raise AgentError(msg)

        package = packages[reference] or {}
        metadata = package.get("metadata") or {}
        return cls(
            name=metadata.get("name") or app.get("name") or "",
            soul=copy.deepcopy(package.get("soul") or {}),
            description=metadata.get("description") or app.get("description") or "",
            role=metadata.get("role") or "",
            icon=metadata.get("icon") or app.get("icon"),
            icon_type=metadata.get("icon_type") or app.get("icon_type") or "emoji",
            icon_background=metadata.get("icon_background")
            or app.get("icon_background"),
            workspace_skills=list(package.get("workspace_skills") or []),
            omitted_assets=list(package.get("omitted_assets") or []),
        )

    @classmethod
    def from_yaml(cls, source: str | Path) -> Agent:
        """Read an Agent from exported YAML, or from a path to it."""
        text = str(source)
        if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source):
            candidate = Path(source)
            if candidate.exists():
                text = candidate.read_text(encoding="utf-8")
        document = yaml.safe_load(text)
        if not isinstance(document, dict):
            msg = "Expected a YAML mapping, which a Dify DSL export always is."
            raise AgentError(msg)
        return cls.from_dict(document)

    # -- writing -----------------------------------------------------------

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        """Render the Agent as a Dify app DSL document.

        Credentials in the soul are blanked unless ``include_secret`` is set,
        so the default output is safe to write to a file and commit.
        """
        if not self.name:
            msg = "An agent needs a name."
            raise AgentError(msg)
        if not self.soul:
            msg = (
                "This agent has an empty soul, so there is nothing to deploy. "
                "Export an agent from Dify with DifyManagement.apps.export(app_id) "
                "and read it with Agent.from_yaml()."
            )
            raise AgentError(msg)

        soul = copy.deepcopy(self.soul)
        package: dict[str, Any] = {
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "metadata": {
                "name": self.name,
                "description": self.description,
                "role": self.role,
                "icon_type": self.icon_type,
                "icon": self.icon,
                "icon_background": self.icon_background,
            },
            "soul": soul if include_secret else portable_soul(soul),
        }
        # Dify's AgentPackage forbids unknown fields, and these two arrived in
        # later releases. Emitting them only when they hold something keeps the
        # document importable by servers that predate them.
        if self.omitted_assets:
            package["omitted_assets"] = list(self.omitted_assets)
        if self.workspace_skills:
            package["workspace_skills"] = list(self.workspace_skills)

        return {
            "app": {
                "name": self.name,
                "mode": self.mode,
                "icon_type": self.icon_type,
                "icon": self.icon,
                "icon_background": self.icon_background,
                "description": self.description,
                "use_icon_as_answer_icon": False,
            },
            "kind": "app",
            "version": DSL_VERSION,
            "agent": {"package_ref": PACKAGE_REF},
            "agent_packages": {PACKAGE_REF: package},
        }

    def to_yaml(
        self,
        path: str | Path | None = None,
        *,
        include_secret: bool = False,
    ) -> str:
        """Render the DSL as YAML, optionally writing it to ``path``."""
        text = yaml.safe_dump(
            self.to_dict(include_secret=include_secret),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        if path is not None:
            Path(path).write_text(text, encoding="utf-8")
        return text

    def __repr__(self) -> str:
        return (
            f"Agent(name={self.name!r}, role={self.role!r}, "
            f"soul_keys={sorted(self.soul)!r})"
        )
