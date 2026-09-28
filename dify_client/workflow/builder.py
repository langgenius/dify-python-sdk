"""Dify apps as code: a workflow or a chatflow, built node by node.

An app is one of the two documents this package writes. It starts where a
caller starts it — a start node, or a trigger Dify fires itself — and ends at
an answer node (a chatflow) or an end node (a workflow), which is what decides
the app mode. Everything in between is the shared graph, which lives on
``GraphDocument``.

The other document built on that graph is a knowledge pipeline, in
``dify_client.workflow.pipeline``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import uuid4

from graphon.nodes.answer.entities import AnswerNodeData
from graphon.nodes.base.entities import OutputVariableEntity
from graphon.nodes.end.entities import EndNodeData
from graphon.nodes.start.entities import StartNodeData
from graphon.variables.input_entities import VariableEntity

from ..agent import AgentError
from ..lifecycle import Stage
from .graph import GraphDocument
from .nodes import (
    NodeInput,
    ScheduleTriggerData,
    TriggerEventNodeData,
    WebhookMethod,
    WebhookParameter,
    WebhookTriggerData,
    agents,
)
from .nodes.triggers import FREQUENCIES
from .parts import ROOT_NODE_TYPES, WorkflowError, _claim_name, _node_rules
from .refs import Node, Ref, Text, VarRef, reference, render
from .results import RunResult

__all__ = ["DSL_VERSION", "WORKFLOW_MODES", "ConversationVar", "Workflow"]

#: The Dify app DSL version this builder emits.
DSL_VERSION = "0.7.0"

#: App modes Dify accepts for a workflow-shaped DSL import.
WORKFLOW_MODES = frozenset({"workflow", "advanced-chat"})

_CHAT_MODES = frozenset({"advanced-chat"})

#: Said in two places, because the mode can be known when the variable is
#: declared or only once the document is finished.
_NO_CONVERSATION = (
    "A {mode!r} app has no conversation to keep {name!r} on, and Dify drops "
    "conversation variables it imports into one. Answer with wf.answer(...) "
    "to make this a chatflow, or use an environment variable instead."
)


class ConversationVar:
    """A variable that survives from one message of a chatflow to the next.

    Environment variables are settings; conversation variables are memory.
    Only ``wf.assign(...)`` writes one, and only a chatflow has a conversation
    for them to live in.
    """

    __slots__ = ("name", "value", "type", "description", "id")

    def __init__(
        self,
        name: str,
        value: Any = "",
        *,
        type: str = "string",
        description: str = "",
        id: str | None = None,
    ):
        self.name = name
        self.value = value
        self.type = type
        self.description = description
        self.id = id or str(uuid4())

    def to_dsl(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "value_type": self.type,
            "value": self.value,
            "description": self.description,
            "selector": ["conversation", self.name],
        }

    def __repr__(self) -> str:
        return f"ConversationVar({self.name!r}, {self.value!r}, type={self.type!r})"


def _packaged(ref: str, agent: Any, *, include_secret: bool) -> dict[str, Any]:
    """One inline Agent, refused in the document's own vocabulary.

    An Agent knows what Dify will not accept from it; it does not know which
    node bound it. A workflow carrying several says which one is wrong, since
    the field an empty Agent is missing is the name you would call it by.
    """
    try:
        return cast(dict[str, Any], agent.to_package(include_secret=include_secret))
    except AgentError as refusal:
        msg = (
            f"The agent bound to this workflow as {ref!r} cannot be shipped: {refusal}"
        )
        raise WorkflowError(msg) from refusal


def _live_console(console: Any, *, api_key: Any, app_id: str | None) -> Any:
    """The console session a live run drafts through, or None to run by key.

    Resolved when the run is asked for rather than beforehand: a console
    passed in, else one from ``DIFY_CONSOLE_TOKEN``. An app key is the other
    route, and says so by being given — or by being the only credential set.
    """
    import os

    from ..console import CONSOLE_TOKEN_ENV, DifyManagement
    from ..secrets import API_KEY_ENV
    from .live import LiveRunError

    if console is not None:
        return console
    if api_key is not None:
        if app_id:
            msg = (
                "app_id names an app to import this definition into, which "
                "needs a console session, and api_key= runs an app as "
                "published. Pass console= (or set DIFY_CONSOLE_TOKEN), or "
                "drop app_id."
            )
            raise LiveRunError(msg)
        return None
    if os.environ.get(CONSOLE_TOKEN_ENV):
        return DifyManagement()
    if os.environ.get(API_KEY_ENV) and not app_id:
        return None
    msg = (
        f"A live run needs a way into Dify. Set {CONSOLE_TOKEN_ENV} (or pass "
        "console=DifyManagement(...)) to run this definition as a draft, or "
        f"{API_KEY_ENV} to run the app behind that key as published."
    )
    raise LiveRunError(msg)


class Workflow(GraphDocument):
    """A Dify app defined in code.

    Example::

        wf = Workflow("greeter")
        start = wf.start([text_input("name")])
        greet = wf.template("Hello, {{ name }}!", variables={"name": start["name"]})
        answer = wf.answer(greet.output)
        wf.connect(start, greet, answer)

        wf.to_yaml("greeter.yml")        # import this into Dify
        wf.run({"name": "Dify"})         # or run it right here

    The node helpers live on ``GraphDocument``, which a knowledge pipeline
    shares. What an app adds is how it starts and ends, the conversation
    variables a chatflow keeps, and running it.
    """

    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        mode: str | None = None,
        icon: str = "🤖",
        icon_background: str = "#FFEAD5",
    ):
        super().__init__(
            name,
            description=description,
            icon=icon,
            icon_background=icon_background,
        )
        self._mode = mode
        #: Inline Agents this document ships, by the ref their nodes bind to.
        self._agent_packages: dict[str, Any] = {}

    @property
    def mode(self) -> str:
        """The Dify app mode, inferred from the nodes unless set explicitly."""
        if self._mode is not None:
            return self._mode
        has_answer = any(n.type == "answer" for n in self._nodes)
        return "advanced-chat" if has_answer else "workflow"

    def conversation_var(
        self,
        name: str,
        value: Any = "",
        *,
        type: str = "string",
        description: str = "",
    ) -> VarRef:
        """Declare a conversation variable, referenced as ``{{#conversation.NAME#}}``.

        It keeps its value across the messages of one conversation, which is
        what gives a chatflow memory beyond the model's context::

            history = wf.conversation_var("seen", [], type="array[string]")
            wf.assign([(history, "append", start["q"])])

        Dify stores these on the conversation, so a ``workflow`` app has
        nowhere to put them — declare them on a chatflow. A document whose
        mode has not been fixed yet is checked when it is rendered instead,
        because an app becomes a chatflow at ``wf.answer(...)``, which is
        usually written after the variables it reads.
        """
        if self._mode is not None and self._mode not in _CHAT_MODES:
            raise WorkflowError(_NO_CONVERSATION.format(mode=self._mode, name=name))
        _claim_name(
            (existing.name for existing in self._conversation_vars),
            name,
            "Conversation variable",
        )
        if value is None:
            msg = (
                f"Conversation variable {name!r} needs a starting value; Dify "
                "rejects one without. Use '' for a string, [] for an array."
            )
            raise WorkflowError(msg)
        self._conversation_vars.append(
            ConversationVar(name, value, type=type, description=description)
        )
        return VarRef("conversation", name)

    @property
    def conversation_vars(self) -> list[ConversationVar]:
        return list(self._conversation_vars)

    def start(
        self,
        inputs: Sequence[VariableEntity] = (),
        *,
        title: str = "Start",
        id: str | None = "start",
    ) -> Node:
        """The entry point, declaring the workflow's input variables."""
        return self.add(StartNodeData(title=title, variables=list(inputs)), id=id)

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
        outputs: Mapping[str, Ref] | None = None,
        *,
        title: str = "End",
        id: str | None = None,
    ) -> Node:
        """A terminal node exposing named workflow outputs."""
        data = EndNodeData(
            title=title,
            outputs=[
                OutputVariableEntity(
                    variable=name,
                    value_selector=reference(ref, f"outputs[{name!r}]").selector,
                )
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
                "A schedule needs exactly one of cron='0 2 * * *' or frequency='daily'."
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

    def plugin_trigger(
        self,
        *,
        plugin_id: str,
        provider_id: str,
        event: str,
        subscription_id: str,
        plugin_unique_identifier: str,
        parameters: Mapping[str, Any] | None = None,
        title: str = "Trigger",
        id: str | None = None,
    ) -> Node:
        """Start the workflow when a plugin reports an event.

        Takes the place of a start node, like ``wf.webhook()``. The plugin and
        the subscription both exist on the server first — Dify mints the
        subscription when the trigger is configured — so every id here is read
        back from it rather than chosen.
        """
        data = TriggerEventNodeData(
            title=title,
            plugin_id=plugin_id,
            provider_id=provider_id,
            event_name=event,
            subscription_id=subscription_id,
            plugin_unique_identifier=plugin_unique_identifier,
            event_parameters={
                key: NodeInput(value=value, type="constant")
                for key, value in (parameters or {}).items()
            },
        )
        return self.add(data, id=id)

    # -- agents that Dify binds to a record -------------------------------
    #
    # Both of these name an Agent through a binding Dify turns into a row while
    # it imports the draft, and only the app importer does that: a pipeline
    # imports the same document, publishes it, and drops `agent_packages` on
    # the way — leaving a node bound to a package that is not there. So they
    # are an app's, and a pipeline keeps `agent()`, whose strategy needs no
    # binding.

    def dify_agent(
        self,
        agent: Any,
        task: str = "",
        *,
        outputs: Sequence[Mapping[str, Any]] = (),
        title: str = "Agent",
        id: str | None = None,
    ) -> Node:
        """An agent node that runs one of the workspace's published Agents.

        ``agent`` is a roster Agent id, or anything carrying one — what
        ``DifyManagement.agents.list()`` returns::

            triage = console.agents.retrieve("support-triage")
            node = wf.dify_agent(triage, "Decide whether this pages someone.",
                                 outputs=[declared_output("severity")])

        The Agent is **shared**: other workflows may bind the same one, and
        publishing a new version of it changes what they all run. To ship an
        Agent that belongs to this workflow alone, use ``wf.inline_agent()``.

        Dify turns the binding into a record while it imports the draft, and
        the Agent must be published and callable from a workflow by then —
        an unpublished one fails the import with "references an unavailable or
        unpublished roster agent".
        """
        with _node_rules():
            data = agents.roster_data(
                agent_id=agent if isinstance(agent, str) else getattr(agent, "id", ""),
                task=task,
                outputs=outputs,
                title=title,
            )
        return self.add(data, id=id)

    def inline_agent(
        self,
        agent: Any,
        task: str = "",
        *,
        outputs: Sequence[Mapping[str, Any]] = (),
        title: str = "Agent",
        id: str | None = None,
    ) -> Node:
        """An agent node that carries its own Agent inside the workflow.

        ``agent`` is a :class:`dify_client.agent.Agent` — the same object
        ``Agent.create(...)`` and ``Agent.from_yaml(...)`` produce — and it is
        exported with the workflow under ``agent_packages``::

            researcher = Agent.create("researcher", instruction="…", model=MODEL)
            node = wf.inline_agent(researcher, "Summarise what you find.")

        Dify creates an Agent owned by this node on import, so the document is
        self-contained: nothing outside it can change what this node runs, and
        deploying the workflow elsewhere takes the Agent along. The trade is
        that it is not the workspace's Agent — edits in the roster do not reach
        it, and it does not appear on the roster.
        """
        package = getattr(agent, "to_package", None)
        if package is None:
            msg = (
                "inline_agent() takes a dify_client.agent.Agent, which carries "
                "the soul to ship. For an Agent the workspace already has, use "
                "wf.dify_agent(agent_id)."
            )
            raise WorkflowError(msg)
        ref = f"agent_{len(self._agent_packages) + 1}"
        # Kept unblanked here and blanked by to_dict() unless the caller asks
        # for secrets, the same way an environment variable is.
        self._agent_packages[ref] = agent
        with _node_rules():
            data = agents.packaged_data(
                package_ref=ref, task=task, outputs=outputs, title=title
            )
        return self.add(data, id=id)

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        """Render the workflow as a Dify app DSL document.

        Secret environment variables are blanked unless ``include_secret`` is
        set, so the default output is safe to write to a file and commit.
        """
        self.validate()
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
            "workflow": self._workflow_section(include_secret=include_secret),
        }
        if self._dependencies:
            document["dependencies"] = self._dependencies
        if self._agent_packages:
            # An inline Agent travels with the workflow: Dify reads these while
            # importing the draft and creates an Agent owned by the node whose
            # binding names the ref.
            document["agent_packages"] = {
                ref: _packaged(ref, agent, include_secret=include_secret)
                for ref, agent in self._agent_packages.items()
            }
        return document

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
        if self._conversation_vars and self.mode not in _CHAT_MODES:
            raise WorkflowError(
                _NO_CONVERSATION.format(
                    mode=self.mode, name=self._conversation_vars[0].name
                )
            )
        terminal = "answer" if self.mode in _CHAT_MODES else "end"
        if not any(n.type == terminal for n in self._nodes):
            msg = (
                f"A {self.mode!r} workflow needs a {terminal!r} node. "
                f"Add one with wf.{terminal}(...)."
            )
            raise WorkflowError(msg)
        edges = self.edges
        connected = {e.source for e in edges} | {e.target for e in edges}
        orphans = [n.id for n in self._nodes if n.id not in connected]
        if orphans and len(self._nodes) > 1:
            msg = (
                f"These nodes are not connected to anything: {', '.join(orphans)}. "
                "A node that reads another node's output is connected by that "
                f"alone; one that reads nothing needs wf.connect(...)."
            )
            raise WorkflowError(msg)
        # A read of a branch names the arm to choose, which says more than
        # "nothing leads here" about the same node, so it is asked first.
        self._check_references_are_reachable()
        self._check_every_node_is_entered()

    def run(
        self,
        inputs: Mapping[str, Any] | None = None,
        *,
        credentials: Mapping[str, Any] | str | None = None,
        llm: Any = None,
        code: Any = None,
        knowledge: Any = None,
        workflow_id: str | None = None,
        raise_on_error: bool = False,
    ) -> RunResult:
        """Run this workflow locally with graphon, without a Dify server.

        Pass ``llm=StubLLM(...)`` to answer every LLM node from a stub, which
        is what makes a workflow testable without provider credentials.

        Pass ``code=LocalSandbox()`` to run code nodes on this machine instead
        of reaching for Dify's sandbox service, or ``code=StubCode({...})`` to
        answer them without running anything.

        Pass ``knowledge=StubKnowledge([...])`` to answer knowledge nodes,
        which graphon cannot run at all: retrieval lives in the server.
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
            if knowledge is not None:
                from .local_knowledge import knowledge_retriever

                stack.enter_context(knowledge_retriever(knowledge))
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

        Unlike ``run(llm=StubLLM(...))``, which is free and offline, this runs
        in Dify — the same engine, plugins and credentials that serve
        production. It proceeds only when ``DIFY_LIVE_TESTS`` is set, so a
        stray call in a test suite cannot start spending.

        **Nothing is published.** The workflow is imported into a temporary
        app, its draft is run the way the editor's Run button runs it, and the
        app is deleted. Testing a definition and releasing it are two acts;
        this is the first, and ``console.apps.deploy(wf)`` is the second::

            result = wf.run_live({"q": "hello"}, max_tokens=2000)
            assert result.node("prompt")["output"].startswith("Summarise")

        The console session comes from ``console=`` or, when that is left
        out, from ``DIFY_CONSOLE_TOKEN`` at the moment of the call.

        ``app_id`` runs the draft of an app that already exists instead —
        where its secret environment variables are set, which a temporary app
        imports blank. Its **draft** is overwritten by this definition; its
        published version is not touched.

        ``api_key`` (or ``DIFY_API_KEY``, with no console token) is the other
        way in: it runs the app behind that key, as published, which is *that
        app* rather than this code — useful to check a release, not to test a
        change.

        A chatflow — anything with an answer node — is driven by a message, so
        pass ``query=``; it arrives as ``sys.query``. The result has the same
        shape a local run produces, so assertions carry across unchanged.
        """
        from .live import LIVE_ENABLED_ENV, LiveRunError, check_budget, live_enabled

        if not live_enabled():
            msg = (
                f"{LIVE_ENABLED_ENV} is not set; live runs call real models and "
                "cost money. Set it to 1 to allow them, or use "
                "run(llm=StubLLM(...)) to test the workflow for free."
            )
            raise LiveRunError(msg)

        console = _live_console(console, api_key=api_key, app_id=app_id)
        if console is not None:
            result = self._run_draft_on(
                console,
                inputs,
                app_id=app_id,
                query=query,
                conversation_id=conversation_id,
            )
        else:
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

    def _run_draft_on(
        self,
        console: Any,
        inputs: Mapping[str, Any] | None,
        *,
        app_id: str | None,
        query: str | None,
        conversation_id: str | None,
    ) -> RunResult:
        """Import this definition as a draft and run it, publishing nothing."""
        from .live import LiveRunError

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

        name = None if app_id else f"{self.name}-run-live-{uuid4().hex[:6]}"
        draft = console.apps.import_definition(self, app_id=app_id, name=name)
        if draft.indeterminate and name:
            # The import may have created the temporary app, and no id came
            # back to delete it by. Its name is this run's alone, so it is
            # found that way rather than left in the workspace unreported.
            from ..resources.management import _sweep_named

            outcome = _sweep_named(console.apps, name)
            msg = (
                f"Dify's answer to the import never arrived ({draft.error}). {outcome}."
            )
            raise LiveRunError(msg)
        try:
            draft.raise_for_stage(Stage.DRAFTED)
            return cast(
                RunResult,
                console.apps.run_draft(
                    draft.app_id or app_id,
                    inputs,
                    query=query,
                    conversation_id=conversation_id,
                ),
            )
        finally:
            # Only an app this call created is this call's to delete; one
            # named by app_id is the caller's, draft and all.
            if not app_id and draft.imported and draft.app_id:
                try:
                    console.apps.delete(draft.app_id)
                # A cleanup failure must not replace the run's own outcome.
                except Exception:  # noqa: BLE001  # nosec B110
                    pass
