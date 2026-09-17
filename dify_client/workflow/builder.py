"""Build Dify workflows in Python and run them locally.

The node schemas come from ``graphon``, the same engine Dify runs in
production, so a workflow built here is validated against the definitions the
server itself uses rather than against a copy that can drift.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml
from graphon.entities.base_node_data import BaseNodeData
from graphon.nodes.answer.entities import AnswerNodeData
from graphon.nodes.base.entities import OutputVariableEntity
from graphon.nodes.code.entities import CodeNodeData
from graphon.nodes.end.entities import EndNodeData
from graphon.nodes.llm.entities import (
    ContextConfig,
    LLMNodeChatModelMessage,
    LLMNodeData,
    ModelConfig,
)
from graphon.nodes.start.entities import StartNodeData
from graphon.nodes.template_transform.entities import TemplateTransformNodeData
from graphon.variables.input_entities import VariableEntity

from ..lifecycle import Stage
from .layout import assign_positions
from .refs import Node, Text, VarRef, render
from .results import RunResult
from .triggers import (
    FREQUENCIES,
    ScheduleTriggerData,
    WebhookMethod,
    WebhookParameter,
    WebhookTriggerData,
)

#: The Dify app DSL version this builder emits.
DSL_VERSION = "0.7.0"

#: App modes Dify accepts for a workflow-shaped DSL import.
WORKFLOW_MODES = frozenset({"workflow", "advanced-chat"})

_CHAT_MODES = frozenset({"advanced-chat"})

#: Node types a graph may begin at. Dify treats each as a root.
ROOT_NODE_TYPES = frozenset(
    {"start", "datasource", "trigger-webhook", "trigger-schedule", "trigger-plugin"}
)


class WorkflowError(Exception):
    """Raised when a workflow is built in a way Dify would reject."""


class EnvVar:
    """A workflow environment variable.

    Marking one ``secret`` keeps its value out of the exported DSL, the same
    way Dify's own export blanks ``SecretVariable`` values unless the caller
    explicitly asks for them. The value is still used by local runs, which
    never touch the disk.
    """

    __slots__ = ("name", "value", "secret", "description", "id")

    def __init__(
        self,
        name: str,
        value: Any = "",
        *,
        secret: bool = False,
        description: str = "",
        id: str | None = None,
    ):
        self.name = name
        self.value = value
        self.secret = secret
        self.description = description
        self.id = id or str(uuid4())

    @property
    def value_type(self) -> str:
        if self.secret:
            return "secret"
        if isinstance(self.value, bool):
            return "string"
        if isinstance(self.value, (int, float)):
            return "number"
        return "string"

    def to_dsl(self, *, include_secret: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "value_type": self.value_type,
            "value": self.value if include_secret or not self.secret else "",
        }

    def __repr__(self) -> str:
        shown = "<secret>" if self.secret else repr(self.value)
        return f"EnvVar({self.name!r}, {shown})"


class Edge:
    """A connection between two nodes."""

    __slots__ = ("source", "target", "source_handle")

    def __init__(self, source: str, target: str, source_handle: str = "source"):
        self.source = source
        self.target = target
        self.source_handle = source_handle


class Workflow:
    """A Dify workflow defined in code.

    Example::

        wf = Workflow("greeter")
        start = wf.start([text_input("name")])
        greet = wf.template("Hello, {{ name }}!", variables={"name": start["name"]})
        answer = wf.answer(greet.output)
        wf.connect(start, greet, answer)

        wf.to_yaml("greeter.yml")        # import this into Dify
        wf.run({"name": "Dify"})         # or run it right here
    """

    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        mode: str | None = None,
        icon: str = "\U0001f916",
        icon_background: str = "#FFEAD5",
    ):
        self.name = name
        self.description = description
        self.icon = icon
        self.icon_background = icon_background
        self._mode = mode
        self._nodes: list[Node] = []
        self._edges: list[Edge] = []
        self._ids: set[str] = set()
        self._dependencies: list[dict[str, Any]] = []
        self._env_vars: list[EnvVar] = []

    # -- structure ---------------------------------------------------------

    @property
    def nodes(self) -> list[Node]:
        return list(self._nodes)

    @property
    def mode(self) -> str:
        """The Dify app mode, inferred from the nodes unless set explicitly."""
        if self._mode is not None:
            return self._mode
        has_answer = any(n.type == "answer" for n in self._nodes)
        return "advanced-chat" if has_answer else "workflow"

    def add(self, data: BaseNodeData, *, id: str | None = None) -> Node:
        """Add any graphon node entity to the workflow.

        Every node type graphon supports is usable through this method, which
        is what the typed helpers below call. Reach for it directly when a node
        has no dedicated helper yet::

            from graphon.nodes.if_else.entities import IfElseNodeData
            branch = wf.add(IfElseNodeData(title="Branch", cases=[...]))
        """
        node_id = self._claim_id(id, data)
        node = Node(id=node_id, data=data)
        self._nodes.append(node)
        return node

    def connect(self, *nodes: Node, handle: str = "source") -> None:
        """Connect nodes in sequence: ``connect(a, b, c)`` links a→b→c.

        ``handle`` names the source branch for nodes that fan out, such as the
        case id of an if-else branch.
        """
        if len(nodes) < 2:
            msg = "connect() needs at least two nodes."
            raise WorkflowError(msg)
        for source, target in zip(nodes, nodes[1:], strict=False):
            self._edges.append(Edge(source.id, target.id, handle))

    def env_var(
        self,
        name: str,
        value: Any = "",
        *,
        secret: bool = False,
        description: str = "",
    ) -> VarRef:
        """Declare an environment variable, referenced as ``{{#env.NAME#}}``.

        ``secret=True`` keeps the value out of ``to_yaml()`` so the exported
        DSL stays safe to commit, while local runs still see it.
        """
        if any(existing.name == name for existing in self._env_vars):
            msg = f"Environment variable {name!r} is already declared."
            raise WorkflowError(msg)
        self._env_vars.append(
            EnvVar(name, value, secret=secret, description=description)
        )
        return VarRef("env", name)

    @property
    def env_vars(self) -> list[EnvVar]:
        return list(self._env_vars)

    def depends_on(self, plugin_identifier: str, *, kind: str = "marketplace") -> None:
        """Declare a plugin this workflow needs, so Dify installs it on import."""
        self._dependencies.append(
            {
                "type": kind,
                "value": {"marketplace_plugin_unique_identifier": plugin_identifier},
            }
        )

    # -- typed node helpers ------------------------------------------------

    def start(
        self,
        inputs: Sequence[VariableEntity] = (),
        *,
        title: str = "Start",
        id: str | None = "start",
    ) -> Node:
        """The entry point, declaring the workflow's input variables."""
        return self.add(StartNodeData(title=title, variables=list(inputs)), id=id)

    def template(
        self,
        template: str,
        *,
        variables: Mapping[str, VarRef] | None = None,
        title: str = "Template",
        id: str | None = None,
    ) -> Node:
        """A Jinja2 template node. Runs locally with no external services."""
        data = TemplateTransformNodeData(
            title=title,
            template=template,
            variables=[
                {"variable": name, "value_selector": ref.selector}
                for name, ref in (variables or {}).items()
            ],
        )
        return self.add(data, id=id)

    def llm(
        self,
        prompt: Text | Sequence[tuple[str, Text]],
        *,
        model: str,
        title: str = "LLM",
        id: str | None = None,
        completion_params: Mapping[str, Any] | None = None,
        mode: str = "chat",
    ) -> Node:
        """An LLM node.

        ``prompt`` is either the text of a single user message or a sequence of
        ``(role, text)`` pairs. Either form accepts variable references, so the
        prompt can be assembled from upstream nodes.

        ``model`` is the fully qualified Dify model reference,
        ``provider/plugin/model``, for example
        ``langgenius/openai/openai:gpt-4o-mini``.
        """
        provider, _, model_name = model.rpartition(":")
        if not provider:
            msg = (
                f"model={model!r} is missing a model name. "
                "Use 'provider/plugin/name:model', "
                "e.g. 'langgenius/openai/openai:gpt-4o-mini'."
            )
            raise WorkflowError(msg)
        if isinstance(prompt, (str, VarRef)):
            messages: list[tuple[str, Text]] = [("user", prompt)]
        else:
            messages = list(prompt)
        data = LLMNodeData(
            title=title,
            model=ModelConfig(
                provider=provider,
                name=model_name,
                mode=mode,
                completion_params=dict(completion_params or {}),
            ),
            prompt_template=[
                LLMNodeChatModelMessage(role=role, text=render(text))
                for role, text in messages
            ],
            context=ContextConfig(enabled=False),
        )
        return self.add(data, id=id)

    def code(
        self,
        code: str,
        *,
        variables: Mapping[str, VarRef] | None = None,
        outputs: Mapping[str, str] | None = None,
        language: str = "python3",
        title: str = "Code",
        id: str | None = None,
    ) -> Node:
        """A code node. Needs a sandbox to run; see ``run(code_executor=...)``."""
        data = CodeNodeData(
            title=title,
            code=code,
            code_language=language,
            variables=[
                {"variable": name, "value_selector": ref.selector}
                for name, ref in (variables or {}).items()
            ],
            outputs={name: {"type": type_} for name, type_ in (outputs or {}).items()},
        )
        return self.add(data, id=id)

    def tool(
        self,
        spec: Any,
        config: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        *,
        title: str | None = None,
        id: str | None = None,
    ) -> Node:
        """Add a tool node from a spec discovered on the workspace.

        ``spec`` comes from ``DifyManagement.tools.catalog()``; it carries the
        identifiers a tool node needs and, crucially, which parameters are
        configured now and which are supplied per run. Passing one as the other
        produces a node Dify accepts and cannot run, so they are separate
        arguments and a misplaced name is rejected here::

            catalog = console.tools.catalog()
            now = wf.tool(catalog["time"]["current_time"],
                          config={"timezone": "Asia/Tokyo"})
            page = wf.tool(catalog["webscraper"]["webscraper"],
                           params={"url": start["url"]})

        A ``params`` value may be a reference to another node, in which case it
        becomes a variable input rather than a constant. The tool's plugin is
        declared automatically, so the deployed app has what it needs.
        """
        from graphon.nodes.tool.entities import ToolNodeData

        config = dict(config or {})
        params = dict(params or {})
        self._check_tool_arguments(spec, config, params)

        data = ToolNodeData(
            title=title or getattr(spec, "label", None) or spec.name,
            provider_id=spec.provider_id,
            provider_type=spec.provider_type,
            provider_name=spec.provider_name,
            tool_name=spec.name,
            tool_label=getattr(spec, "label", None) or spec.name,
            tool_configurations=config,
            tool_parameters={
                name: _tool_input(value) for name, value in params.items()
            },
            plugin_unique_identifier=getattr(spec, "plugin_unique_identifier", None),
        )
        node = self.add(data, id=id)
        identifier = getattr(spec, "plugin_unique_identifier", None)
        if identifier:
            self.depends_on(identifier)
        return node

    def _check_tool_arguments(
        self,
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
                    msg = (
                        f"{spec.name!r} has no parameter {name!r}. "
                        f"It takes: {available}."
                    )
                    raise WorkflowError(msg)
                if parameter.is_configuration is not expected:
                    other = "params" if expected else "config"
                    kind = "supplied per run" if expected else "configured when built"
                    msg = (
                        f"{name!r} is {kind}, so it belongs in {other}=, not "
                        f"{group}=. Dify accepts the node either way and then "
                        "cannot run it."
                    )
                    raise WorkflowError(msg)

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
            raise WorkflowError(msg)

    def answer(
        self,
        answer: Text,
        *,
        title: str = "Answer",
        id: str | None = None,
    ) -> Node:
        """A streaming answer node. Its presence makes the app a chatflow."""
        return self.add(AnswerNodeData(title=title, answer=render(answer)), id=id)

    def end(
        self,
        outputs: Mapping[str, VarRef] | None = None,
        *,
        title: str = "End",
        id: str | None = None,
    ) -> Node:
        """A terminal node exposing named workflow outputs."""
        data = EndNodeData(
            title=title,
            outputs=[
                OutputVariableEntity(variable=name, value_selector=ref.selector)
                for name, ref in (outputs or {}).items()
            ],
        )
        return self.add(data, id=id)

    def webhook(
        self,
        *,
        method: str = "post",
        headers: Sequence[WebhookParameter] = (),
        params: Sequence[WebhookParameter] = (),
        body: Sequence[WebhookParameter] = (),
        content_type: str = "application/json",
        status_code: int = 200,
        response_body: str = "",
        title: str = "Webhook",
        id: str | None = None,
    ) -> Node:
        """Start the workflow when its webhook URL is called.

        Takes the place of a start node. Each declared field becomes an output
        of this node::

            from dify_client.workflow import body_field, header

            hook = wf.webhook(body=[body_field("order_id", required=True)])
            wf.connect(hook, wf.end({"id": hook["order_id"]}))

        Dify mints the URL on publish, not on import — read it back with
        ``ConsoleClient.webhook_trigger(app_id, node_id)``.
        """
        data = WebhookTriggerData(
            title=title,
            method=cast(WebhookMethod, method.lower()),
            content_type=content_type,
            headers=list(headers),
            params=list(params),
            body=list(body),
            status_code=status_code,
            response_body=response_body,
        )
        return self.add(data, id=id)

    def schedule(
        self,
        cron: str | None = None,
        *,
        frequency: str | None = None,
        at: Mapping[str, Any] | None = None,
        timezone: str = "UTC",
        title: str = "Schedule",
        id: str | None = None,
    ) -> Node:
        """Start the workflow on a clock, in place of a start node.

        Either give a cron expression::

            wf.schedule("0 2 * * *", timezone="Asia/Tokyo")

        or a frequency with the detail the Dify editor shows::

            wf.schedule(frequency="weekly", at={"time": "9:00 AM", "weekdays": ["mon"]})

        The two are the same trigger to Dify; the difference is that the editor
        can render the second as a form and only shows the first as text.
        """
        if (cron is None) == (frequency is None):
            msg = (
                "A schedule needs exactly one of cron='0 2 * * *' or "
                "frequency='daily'."
            )
            raise WorkflowError(msg)
        if frequency is not None and frequency not in FREQUENCIES:
            accepted = ", ".join(FREQUENCIES)
            msg = f"frequency={frequency!r} is not one Dify knows. Use one of: {accepted}."
            raise WorkflowError(msg)
        if cron is not None and at is not None:
            msg = "at=... describes a frequency; a cron expression already says when."
            raise WorkflowError(msg)

        data = ScheduleTriggerData(
            title=title,
            mode="cron" if cron is not None else "visual",
            cron_expression=cron,
            frequency=frequency,
            visual_config=dict(at) if at is not None else None,
            timezone=timezone,
        )
        return self.add(data, id=id)

    # -- serialisation -----------------------------------------------------

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        """Render the workflow as a Dify app DSL document.

        Secret environment variables are blanked unless ``include_secret`` is
        set, so the default output is safe to write to a file and commit.
        """
        self.validate()
        positions = assign_positions(
            [n.id for n in self._nodes],
            [(e.source, e.target) for e in self._edges],
        )
        types = {n.id: n.type for n in self._nodes}
        document: dict[str, Any] = {
            "app": {
                "name": self.name,
                "mode": self.mode,
                "icon_type": "emoji",
                "icon": self.icon,
                "icon_background": self.icon_background,
                "description": self.description,
                "use_icon_as_answer_icon": False,
            },
            "kind": "app",
            "version": DSL_VERSION,
            "workflow": {
                "graph": {
                    "nodes": [
                        {
                            "id": node.id,
                            "type": "custom",
                            "position": positions[node.id],
                            "data": node.data.model_dump(
                                mode="json", exclude_none=True
                            ),
                        }
                        for node in self._nodes
                    ],
                    "edges": [
                        {
                            "id": f"{edge.source}-{edge.source_handle}-{edge.target}",
                            "source": edge.source,
                            "target": edge.target,
                            "sourceHandle": edge.source_handle,
                            "targetHandle": "target",
                            "type": "custom",
                            "data": {
                                "sourceType": types[edge.source],
                                "targetType": types[edge.target],
                                "isInIteration": False,
                            },
                        }
                        for edge in self._edges
                    ],
                    "viewport": {"x": 0, "y": 0, "zoom": 1},
                },
                "features": {},
                "environment_variables": [
                    var.to_dsl(include_secret=include_secret) for var in self._env_vars
                ],
                "conversation_variables": [],
            },
        }
        if self._dependencies:
            document["dependencies"] = self._dependencies
        return document

    def to_yaml(
        self,
        path: str | Path | None = None,
        *,
        include_secret: bool = False,
    ) -> str:
        """Render the DSL as YAML, optionally writing it to ``path``.

        Secret environment variables are blanked by default. Only pass
        ``include_secret=True`` for output that is going somewhere as guarded
        as the secrets themselves.
        """
        text = yaml.safe_dump(
            self.to_dict(include_secret=include_secret),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        if path is not None:
            Path(path).write_text(text, encoding="utf-8")
        return text

    def validate(self) -> None:
        """Check the structure Dify requires, raising ``WorkflowError``."""
        if not self._nodes:
            msg = "Workflow has no nodes."
            raise WorkflowError(msg)
        starts = [n for n in self._nodes if n.type == "start"]
        roots = [n for n in self._nodes if n.type in ROOT_NODE_TYPES]
        if not roots:
            msg = (
                "Workflow has nothing to start from. Add wf.start([...]), or a "
                "trigger with wf.webhook(...) or wf.schedule(...)."
            )
            raise WorkflowError(msg)
        if len(starts) > 1:
            ids = ", ".join(n.id for n in starts)
            msg = f"Workflow has more than one start node: {ids}."
            raise WorkflowError(msg)
        if self.mode not in WORKFLOW_MODES:
            accepted = ", ".join(sorted(WORKFLOW_MODES))
            msg = f"mode={self.mode!r} is not importable as a workflow. Use one of: {accepted}."
            raise WorkflowError(msg)
        terminal = "answer" if self.mode in _CHAT_MODES else "end"
        if not any(n.type == terminal for n in self._nodes):
            msg = (
                f"A {self.mode!r} workflow needs a {terminal!r} node. "
                f"Add one with wf.{terminal}(...)."
            )
            raise WorkflowError(msg)
        connected = {e.source for e in self._edges} | {e.target for e in self._edges}
        orphans = [n.id for n in self._nodes if n.id not in connected]
        if orphans and len(self._nodes) > 1:
            msg = (
                f"These nodes are not connected to anything: {', '.join(orphans)}. "
                "Link them with wf.connect(...)."
            )
            raise WorkflowError(msg)

    # -- local execution ---------------------------------------------------

    def run(
        self,
        inputs: Mapping[str, Any] | None = None,
        *,
        credentials: Mapping[str, Any] | str | None = None,
        llm: Any = None,
        code: Any = None,
        workflow_id: str | None = None,
        raise_on_error: bool = False,
    ) -> RunResult:
        """Run this workflow locally with graphon, without a Dify server.

        Pass ``llm=StubLLM(...)`` to answer every LLM node from a stub, which
        is what makes a workflow testable without provider credentials.

        Pass ``code=LocalSandbox()`` to run code nodes on this machine instead
        of reaching for Dify's sandbox service, or ``code=StubCode({...})`` to
        answer them without running anything.
        """
        from contextlib import ExitStack

        from .runner import run_dsl

        # A local run stays in memory, so it gets the real secret values that
        # the on-disk export deliberately omits.
        dsl = self.to_yaml(include_secret=True)
        run_id = workflow_id or self.name

        with ExitStack() as stack:
            if llm is not None:
                from .testing import stub_models

                stack.enter_context(stub_models(llm))
            if code is not None:
                from .sandbox import code_executor

                stack.enter_context(code_executor(code))
            result = run_dsl(
                dsl,
                inputs=inputs,
                credentials=None if llm is not None else credentials,
                workflow_id=run_id,
            )
        return result.raise_for_status() if raise_on_error else result

    def run_live(
        self,
        inputs: Mapping[str, Any] | None = None,
        *,
        api_key: Any = None,
        base_url: str | None = None,
        console: Any = None,
        app_id: str | None = None,
        query: str | None = None,
        conversation_id: str | None = None,
        max_cost: str | float | None = None,
        max_tokens: int | None = None,
        user: str = "dify-python-sdk",
        raise_on_error: bool = True,
    ) -> RunResult:
        """Run this workflow on a real Dify instance. **This costs money.**

        Unlike ``run(llm=StubLLM(...))``, which is free and offline, this calls
        the app in Dify — the same engine, plugins, credentials and version
        that serve production. It proceeds only when ``DIFY_LIVE_TESTS`` is
        set, so a stray call in a test suite cannot start spending.

        Pass ``console`` (a ``DifyManagement``) and ``app_id`` to deploy this
        workflow over that app first. Doing so is what keeps the test about
        *this code* rather than about whatever the app in Dify has drifted
        into::

            result = wf.run_live(
                {"q": "hello"},
                console=ConsoleClient(), app_id=APP_ID,
                max_tokens=2000,
            )
            assert result.node("prompt")["output"].startswith("Summarise")

        A chatflow — anything with an answer node — is driven by a message, so
        pass ``query=``; it arrives as ``sys.query``. A ``workflow`` app runs on
        ``inputs`` alone. The Service API serves the two at different paths and
        rejects the wrong one, so the route follows ``self.mode``.

        The result has the same shape a local run produces, so assertions carry
        across unchanged.
        """
        from .live import (
            LIVE_ENABLED_ENV,
            LiveRunError,
            check_budget,
            live_enabled,
        )

        if not live_enabled():
            msg = (
                f"{LIVE_ENABLED_ENV} is not set; live runs call real models and "
                "cost money. Set it to 1 to allow them, or use "
                "run(llm=StubLLM(...)) to test the workflow for free."
            )
            raise LiveRunError(msg)

        if console is not None:
            if not app_id:
                msg = (
                    "Deploying needs app_id: which app in Dify to overwrite. "
                    "Create the app once, then pass its id so every run tests "
                    "the workflow this code defines."
                )
                raise LiveRunError(msg)
            missing = self.missing_plugin_dependencies()
            if missing:
                names = ", ".join(missing)
                msg = (
                    f"This workflow uses {names} but declares no plugin for it, "
                    "so Dify would import an app it cannot run. Declare it with "
                    'wf.depends_on("<plugin>:<version>@<hash>") — the identifier '
                    "is on the plugin's Dify Marketplace page."
                )
                raise LiveRunError(msg)
            # Importing a DSL writes a draft; the Service API runs the
            # published version. Deploying does both and reports which step it
            # reached, so a publish that failed is not mistaken for a run of
            # the wrong version.
            console.apps.deploy(self, app_id=app_id, key=False).raise_for_stage(
                Stage.PUBLISHED
            )
        elif app_id:
            msg = "app_id was given without console=..., so there is nothing to deploy with."
            raise LiveRunError(msg)

        from .dify_runner import run_on_dify

        result = run_on_dify(
            inputs,
            api_key=api_key,
            base_url=base_url,
            mode=self.mode,
            query=query,
            conversation_id=conversation_id,
            user=user,
        )
        if raise_on_error:
            result.raise_for_status()
        return check_budget(result, max_cost, max_tokens)

    def missing_plugin_dependencies(self) -> list[str]:
        """Model providers this workflow uses but never declared a plugin for.

        Dify installs a workflow's declared plugins when it imports the DSL, so
        an undeclared provider deploys into an app that cannot run.
        """
        declared = {
            str(dep.get("value", {}).get("marketplace_plugin_unique_identifier", ""))
            for dep in self._dependencies
        }
        missing: list[str] = []
        for node in self._nodes:
            provider = getattr(getattr(node.data, "model", None), "provider", "")
            if not provider:
                continue
            plugin = "/".join(str(provider).split("/")[:2])
            if not any(name.startswith(f"{plugin}:") for name in declared):
                if plugin not in missing:
                    missing.append(plugin)
        return missing

    # -- internals ---------------------------------------------------------

    def _claim_id(self, requested: str | None, data: BaseNodeData) -> str:
        base = requested or str(data.type).replace("-", "_")
        if requested is not None and requested in self._ids:
            msg = f"Node id {requested!r} is already used in this workflow."
            raise WorkflowError(msg)
        node_id = base
        counter = 2
        while node_id in self._ids:
            node_id = f"{base}_{counter}"
            counter += 1
        self._ids.add(node_id)
        return node_id


def _tool_input(value: Any) -> dict[str, Any]:
    """Wrap a runtime argument as Dify's tool input, constant or variable."""
    if isinstance(value, VarRef):
        return {"type": "variable", "value": value.selector}
    if isinstance(value, Node):
        return {"type": "variable", "value": value.output.selector}
    if isinstance(value, str) and "{{#" in value:
        return {"type": "mixed", "value": value}
    return {"type": "constant", "value": value}


def load_yaml(source: str | Path) -> dict[str, Any]:
    """Read a Dify DSL document from a path or a YAML string."""
    path = Path(source) if isinstance(source, (str, Path)) else None
    if path is not None and path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return yaml.safe_load(str(source))


def _iter_ids(nodes: Iterable[Node]) -> list[str]:
    return [n.id for n in nodes]
