"""The graph both Dify documents are: nodes, edges, and the helpers that add them.

An app and a knowledge pipeline differ at their ends and in their envelope;
between those ends they are the same graph, built with the same helpers. That
graph is :class:`GraphDocument`, and the two documents are
:class:`dify_client.workflow.builder.Workflow` and
:class:`dify_client.workflow.pipeline.Pipeline`.

The node schemas come from ``graphon``, the same engine Dify runs in
production, so what is built here is validated against the definitions the
server itself uses rather than against a copy that can drift. The types Dify
implements and graphon does not come from :mod:`dify_client.workflow.nodes`,
and the pieces a graph is assembled from — variables, edges, containers,
arguments — from :mod:`dify_client.workflow.parts`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import yaml
from graphon.entities.base_node_data import BaseNodeData
from graphon.nodes.code.entities import CodeNodeData
from graphon.nodes.document_extractor.entities import DocumentExtractorNodeData
from graphon.nodes.http_request.entities import HttpRequestNodeAuthorization
from graphon.nodes.iteration.entities import IterationNodeData, IterationStartNodeData
from graphon.nodes.list_operator.entities import FilterCondition
from graphon.nodes.loop.entities import (
    LoopNodeData,
    LoopStartNodeData,
    LoopVariableData,
)
from graphon.nodes.parameter_extractor.entities import ParameterConfig
from graphon.nodes.template_transform.entities import TemplateTransformNodeData

from .conditions import Condition
from .layout import COLUMN_WIDTH, assign_positions
from .nodes import (
    DEFAULT_TOP_K,
    FormInputConfig,
    MetadataFilteringCondition,
    NodeInput,
    UserActionConfig,
    agents,
    human_input,
    knowledge,
    logic,
    models,
    tools,
)
from .nodes import http as http_node
from .parts import (
    _CANVAS_TYPES,
    CONTAINER_HEIGHT,
    NESTED_Z_INDEX,
    ROOT_NODE_TYPES,
    Container,
    ContainerT,
    Edge,
    EnvVar,
    Iteration,
    Loop,
    NodeT,
    WorkflowError,
    _claim_name,
    _edge_data,
    _node_input,
    _node_retrieval,
    _node_rules,
    _search_settings,
    _split_model,
)
from .refs import Branch, Handle, Node, Ref, Text, VarRef, reference

#: Node types a graph may begin at. Dify treats each as a root.
#: How a reference to another node's output reads inside a string.
_TEMPLATE_REF = re.compile(r"\{\{#([A-Za-z0-9_-]+)\.")

# Fields whose list value is a variable selector. Other lists are ordinary
# data — select options and plugin arguments may legitimately begin with text
# that happens to equal a node id.
_SELECTOR_FIELDS = frozenset(
    {
        "index_chunk_variable_selector",
        "iterator_selector",
        "output_selector",
        "query",
        "query_attachment_selector",
        "query_variable_selector",
        "selector",
        "value_selector",
        "variable",
        "variable_selector",
        "variables",
    }
)


def _is_branch(node: Node | None) -> bool:
    """Whether a node continues along named arms rather than one way out."""
    return isinstance(node, Branch) and bool(node.handles)


def _arm(branch: Branch, handle: str) -> str:
    """The arm an edge leaves a branch by, refused when it is not one.

    An edge out of a branch with the default handle, or with a name that is
    not an arm, imports and publishes and is never taken.
    """
    if handle == "source":
        arms = ", ".join(repr(arm) for arm in branch.handles)
        msg = (
            f"{branch.id!r} is a branch, so an edge out of it needs an arm: "
            f"wf.connect({branch.id}.case(...), ...). Its arms are {arms}."
        )
        raise WorkflowError(msg)
    try:
        return branch.case(handle).name
    except KeyError as refusal:
        raise WorkflowError(refusal.args[0]) from refusal


class GraphDocument:
    """A graph of Dify nodes, and the parts of a DSL document that hold one.

    Two documents are built from it and they are different kinds: a
    ``Workflow`` is an app, a ``Pipeline`` is a knowledge pipeline. What they
    share is everything between the ends — the nodes, the edges, the canvas,
    the environment variables and the plugins a document declares — so that
    lives here, and each subclass adds the ends that are its own.

    Subclasses supply ``to_dict()`` and ``validate()``: the envelope around
    the graph, and what that kind of document requires of it. Nothing here
    knows which kind it is in, which is what keeps an answer node out of a
    pipeline and a datasource out of neither.
    """

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        """Render this document, envelope and all. Defined by the subclass."""
        raise NotImplementedError

    def validate(self) -> None:
        """Check what this kind of document requires. Defined by the subclass."""
        raise NotImplementedError

    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        icon: str = "\U0001f916",
        icon_background: str = "#FFEAD5",
    ):
        self.name = name
        self.description = description
        self.icon = icon
        self.icon_background = icon_background
        self._nodes: list[Node] = []
        self._edges: list[Edge] = []
        self._ids: set[str] = set()
        self._parents: dict[str, str] = {}
        self._containers: dict[str, str] = {}
        #: Each container's start marker, by the container it belongs to.
        self._container_starts: dict[str, str] = {}
        #: Containers whose block has closed, in the order they closed; only
        #: those have a body to wire the start marker into.
        self._closed: list[str] = []
        self._container_stack: list[str] = []
        self._dependencies: list[dict[str, Any]] = []
        self._env_vars: list[EnvVar] = []
        # Only an app declares these, but both documents carry the key,
        # so the list lives with the serializer that writes it.
        self._conversation_vars: list[Any] = []

    @property
    def nodes(self) -> list[Node]:
        return list(self._nodes)

    def add(self, data: BaseNodeData, *, id: str | None = None) -> Node:
        """Add any graphon node entity to the workflow.

        Every node type graphon supports is usable through this method, which
        is what the typed helpers below call. Reach for it directly when a node
        has no dedicated helper yet::

            from graphon.nodes.if_else.entities import IfElseNodeData
            branch = wf.add(IfElseNodeData(title="Branch", cases=[...]))
        """
        return self._add_as(Node, data, id)

    def _add_as(self, kind: type[NodeT], data: BaseNodeData, id: str | None) -> NodeT:
        """Add a node, as the kind of handle it turns out to be.

        A node that fans out comes back as a :class:`Branch`, which names its
        arms; everything else is a plain :class:`Node`.
        """
        node_id = self._claim_id(id, data)
        node = kind(id=node_id, data=data)
        self._nodes.append(node)
        if self._container_stack:
            self._parents[node_id] = self._container_stack[-1]
        return node

    def connect(self, *nodes: Node | Handle, handle: str = "source") -> None:
        """Connect nodes in sequence: ``connect(a, b, c)`` links a→b→c.

        Most edges need not be written at all: a node that reads another
        node's output is already saying it runs after it, and
        :meth:`to_dict` derives those. What is left is control flow, which no
        reference implies — which arm of a branch to take::

            wf.connect(branch.true, escalate)
            wf.connect(branch.false, queue)

        ``handle`` is the same thing spelled as a string, for a branch whose
        arms this SDK does not name.
        """
        if len(nodes) < 2:
            msg = "connect() needs at least two nodes."
            raise WorkflowError(msg)
        for source, target in zip(nodes, nodes[1:], strict=False):
            name = source.name if isinstance(source, Handle) else handle
            if isinstance(source, Branch) and source.handles:
                name = _arm(source, name)
            self._edges.append(Edge(source.id, target.id, name))

    # -- what the references already said -----------------------------------

    def _referenced(self, node: Node) -> set[str]:
        """The nodes this one reads from, read out of what it was built with.

        Both spellings count: a selector (``["llm", "text"]``) and a template
        (``{{#llm.text#}}``). A first segment that is not a node in this
        document is a namespace — ``sys``, ``env``, ``conversation``, ``rag``
        — and names no edge.
        """
        known = {other.id for other in self._nodes} - {node.id}
        found: set[str] = set()

        def walk(value: Any, *, selector: bool = False) -> None:
            if isinstance(value, str):
                found.update(
                    match for match in _TEMPLATE_REF.findall(value) if match in known
                )
            elif isinstance(value, Mapping):
                input_kind = value.get("type")
                value_kind = value.get("input_type") or value.get("value_type")
                for key, item in value.items():
                    # A typed constant's value is opaque. Looking inside it
                    # made ["start", "literal"] an edge from the start node.
                    if key == "value" and input_kind == "constant":
                        continue
                    is_selector = key in _SELECTOR_FIELDS or key.endswith("_selector")
                    if key == "value" and (
                        input_kind == "variable" or value_kind == "variable"
                    ):
                        is_selector = True
                    walk(item, selector=is_selector)
            elif isinstance(value, (list, tuple)):
                if selector and value and isinstance(value[0], str):
                    # A selector: the first segment is the node it reads.
                    if value[0] in known:
                        found.add(value[0])
                    return
                for item in value:
                    walk(item, selector=selector)

        walk(node.data.model_dump(mode="json"))
        return found

    def _inferred_edges(self) -> list[Edge]:
        """The edges the graph already implies, for the nodes nobody wired.

        Each of these was a bug before it was a rule:

        **A node whose inbound edges were written by hand is wired by hand.**
        An arm of a branch usually reads something from before the branch —
        the message it is triaging — and that is a reference, not a second way
        in. Inferring one there gives the arm a path that skips the branch, so
        both arms run.

        **No inferred edge goes around a branch.** The same bypass, one node
        further down: a node after an arm that also reads the start node got
        an edge from it, and Dify skips a node only when *every* way in was
        skipped, so it ran whichever arm was taken. An edge is dropped when
        its target sits after a branch its source does not.

        **An edge is only inferred inside one container.** A node in an
        iteration may read a variable from outside it; that is a reference
        too, not a step in the body.

        **Nothing is inferred out of a branch.** A branch continues along the
        arm it chose, and an inferred edge has no arm: it was written with the
        handle ``source``, which Dify never takes, so everything after it
        silently never ran. Which arm leads to a node is a decision only the
        caller can make; ``validate()`` asks for it.
        """
        wired = {edge.target for edge in self._edges}
        written = {(edge.source, edge.target) for edge in self._edges}
        branches = {node.id for node in self._nodes if _is_branch(node)}
        inferred: list[Edge] = []
        for node in self._nodes:
            if node.id in wired:
                continue
            for source in sorted(self._referenced(node)):
                if (source, node.id) in written or source in branches:
                    continue
                if self._parents.get(source) != self._parents.get(node.id):
                    continue
                written.add((source, node.id))
                inferred.append(Edge(source, node.id))
        # Dropping an edge only ever removes a branch from what lies after it,
        # so this settles; the reachability check then says whether what is
        # left still orders every read.
        while True:
            after = self._after_branches([*self._edges, *inferred], branches)
            kept = [e for e in inferred if after[e.target] <= after[e.source]]
            if len(kept) == len(inferred):
                return inferred
            inferred = kept

    def _after_branches(
        self, edges: Sequence[Edge], branches: set[str]
    ) -> dict[str, set[str]]:
        """For each node, the branches some path into it passes through."""
        into: dict[str, list[str]] = {}
        for edge in edges:
            into.setdefault(edge.target, []).append(edge.source)
        after: dict[str, set[str]] = {node.id: set() for node in self._nodes}
        changed = True
        while changed:
            changed = False
            for target, sources in into.items():
                reached = set()
                for source in sources:
                    reached |= after.get(source, set())
                    if source in branches:
                        reached.add(source)
                if not reached <= after.setdefault(target, set()):
                    after[target] |= reached
                    changed = True
        return after

    @property
    def edges(self) -> list[Edge]:
        """Every edge this document will carry, written and inferred alike."""
        known = [*self._edges, *self._inferred_edges()]
        return [*known, *self._start_edges(known)]

    def _check_every_node_is_entered(self) -> None:
        """Raise when a node has a way out but no way in.

        Such a node is connected, so the orphan check passes it, and it still
        never runs: nothing reaches it. Only what the run starts from — a start
        node, a trigger, a datasource, a container's start marker — may have
        nothing leading into it.
        """
        entered = {edge.target for edge in self.edges}
        markers = set(self._container_starts.values())
        stranded = [
            node.id
            for node in self._nodes
            if node.id not in entered
            and node.type not in ROOT_NODE_TYPES
            and node.id not in markers
        ]
        if stranded:
            msg = (
                f"Nothing leads into {', '.join(stranded)}, so it never runs. "
                "Connect something to it, or have it read a node that runs "
                "before it."
            )
            raise WorkflowError(msg)

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
        _claim_name(
            (existing.name for existing in self._env_vars),
            name,
            "Environment variable",
        )
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

    def template(
        self,
        template: str,
        *,
        variables: Mapping[str, Ref] | None = None,
        title: str = "Template",
        id: str | None = None,
    ) -> Node:
        """A Jinja2 template node. Runs locally with no external services."""
        data = TemplateTransformNodeData(
            title=title,
            template=template,
            variables=[
                {
                    "variable": name,
                    "value_selector": reference(ref, f"variables[{name!r}]").selector,
                }
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
        if isinstance(prompt, (str, VarRef, Node)):
            messages: list[tuple[str, Text]] = [("user", prompt)]
        else:
            messages = list(prompt)
        with _node_rules():
            data = models.llm_data(
                messages=messages,
                model=_split_model(model, "model"),
                mode=mode,
                completion_params=completion_params,
                title=title,
            )
        return self.add(data, id=id)

    def code(
        self,
        code: str,
        *,
        variables: Mapping[str, Ref] | None = None,
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
                {
                    "variable": name,
                    "value_selector": reference(ref, f"variables[{name!r}]").selector,
                }
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
        with _node_rules():
            data = tools.tool_data(
                spec=spec,
                config=dict(config or {}),
                parameters={
                    name: _node_input(value) for name, value in (params or {}).items()
                },
                title=title,
            )
        node = self.add(data, id=id)
        identifier = getattr(spec, "plugin_unique_identifier", None)
        if identifier:
            self.depends_on(identifier)
        return node

    def knowledge(
        self,
        query: Ref,
        datasets: Sequence[str],
        *,
        top_k: int | None = None,
        score_threshold: float | None = None,
        rerank: str | None = None,
        weights: Mapping[str, Any] | None = None,
        mode: str = "multiple",
        model: str | None = None,
        metadata: MetadataFilteringCondition | None = None,
        title: str = "Knowledge",
        id: str | None = None,
    ) -> Node:
        """Retrieve from knowledge bases, the node Dify calls Knowledge Retrieval.

        ``query`` is the text to search for and ``datasets`` are workspace
        knowledge base ids — ``DifyKnowledge.datasets.list()`` shows what is
        there::

            hits = wf.knowledge(start["question"], [DATASET_ID], top_k=3)
            reply = wf.llm([("system", f"Use only: {hits.output}"),
                            ("user", start["question"])], model=MODEL)

        The node's ``result`` is an array of objects with ``content``,
        ``title`` and ``metadata``, which is what ``hits.output`` refers to.

        Two retrieval modes, as Dify names them. ``multiple`` searches every
        listed base and merges; pass ``rerank=`` to score the merge with a
        rerank model. ``single`` has a model choose one base first, so it needs
        ``model=`` — and carries no retrieval settings of its own, so
        ``top_k``, ``score_threshold``, ``rerank`` and ``weights`` are refused
        there rather than written somewhere Dify does not read. What a single
        search returns is whatever the chosen knowledge base is configured for.

        Retrieval happens inside Dify, so this node has no graphon
        implementation: ``wf.run()`` needs ``knowledge=StubKnowledge(...)`` to
        answer it, and what a real index returns is only visible through
        ``wf.run_live()`` or ``DifyKnowledge.datasets.search()``.

        Keep this code, not an export, as the source of truth for which base a
        workflow reads: Dify encrypts ``dataset_ids`` on export, keyed by the
        workspace, so an exported document neither names the base nor carries
        it to another workspace — imported there the id decrypts to nothing
        and is dropped, leaving a knowledge node with no knowledge base.
        """
        with _node_rules():
            knowledge.check_retrieval_mode(
                mode=mode,
                model=model,
                rerank=rerank,
                weights=weights,
                top_k=top_k,
                score_threshold=score_threshold,
            )
            data = knowledge.retrieval_data(
                query=reference(query, "query").selector,
                datasets=datasets,
                model=_split_model(model, "model") if model is not None else None,
                settings=(
                    None
                    if model is not None
                    else _node_retrieval(
                        top_k=DEFAULT_TOP_K if top_k is None else top_k,
                        score_threshold=score_threshold,
                        rerank=rerank,
                        weights=weights,
                    )
                ),
                metadata=metadata,
                title=title,
            )
        return self.add(data, id=id)

    def if_else(
        self,
        conditions: Sequence[Condition] | Mapping[str, Sequence[Condition]],
        *,
        logical: str = "and",
        title: str = "IF/ELSE",
        id: str | None = None,
    ) -> Branch:
        """Branch on conditions. Connect the outcomes by handle::

            from dify_client.workflow import when

            branch = wf.if_else([when(start["score"], ">", "80")])
            wf.connect(start, branch)
            wf.connect(branch, pass_, handle="true")
            wf.connect(branch, fail, handle="false")

        A mapping builds the ELIF chain Dify's editor draws, one case per key,
        and the keys are the handles::

            wf.if_else({"big": [...], "small": [...]})   # and "false"
        """
        with _node_rules():
            data = logic.if_else_data(
                conditions=conditions, logical=logical, title=title
            )
        return self._add_as(Branch, data, id)

    def http(
        self,
        url: Text,
        *,
        method: str = "get",
        headers: Mapping[str, Text] | str | None = None,
        params: Mapping[str, Text] | str | None = None,
        json: Any = None,
        text: Text | None = None,
        form: Mapping[str, Text] | None = None,
        auth: HttpRequestNodeAuthorization | None = None,
        timeout: tuple[int, int, int] | None = None,
        ssl_verify: bool = True,
        title: str = "HTTP Request",
        id: str | None = None,
    ) -> Node:
        """Call an HTTP API. Its ``body``, ``status_code`` and ``headers`` are outputs::

            page = wf.http("https://api.example.com/orders/{{#start.id#}}",
                           headers={"Accept": "application/json"},
                           auth=bearer(wf.env_var("TOKEN", secret=True)))

        At most one body: ``json=`` (a mapping is serialised), ``text=`` for a
        raw string, ``form=`` for form fields. ``timeout`` is
        ``(connect, read, write)`` in seconds.
        """
        with _node_rules():
            data = http_node.request_data(
                url=url,
                method=method,
                headers=headers,
                params=params,
                json=json,
                text=text,
                form=form,
                auth=auth,
                timeout=timeout,
                ssl_verify=ssl_verify,
                title=title,
            )
        return self.add(data, id=id)

    def classify(
        self,
        query: Ref,
        classes: Sequence[str] | Mapping[str, str],
        *,
        model: str,
        instruction: str | None = None,
        title: str = "Question Classifier",
        id: str | None = None,
    ) -> Branch:
        """Route by what a model makes of a variable.

        Each class is a branch, and its id is the handle to connect::

            kind = wf.classify(start["q"], ["a refund request", "anything else"],
                               model=MODEL)
            wf.connect(kind, refunds, handle="1")
            wf.connect(kind, general, handle="2")

        A mapping chooses the ids instead of numbering them, which keeps the
        handles readable: ``{"refund": "a refund request", ...}``.
        """
        if isinstance(classes, Mapping):
            pairs = list(classes.items())
        else:
            pairs = [(str(index), name) for index, name in enumerate(classes, start=1)]
        with _node_rules():
            data = models.classifier_data(
                query=reference(query, "query").selector,
                classes=pairs,
                model=_split_model(model, "model"),
                instruction=instruction,
                title=title,
            )
        return self._add_as(Branch, data, id)

    def extract_parameters(
        self,
        query: Ref,
        parameters: Sequence[ParameterConfig],
        *,
        model: str,
        instruction: str | None = None,
        reasoning: str = "prompt",
        title: str = "Parameter Extractor",
        id: str | None = None,
    ) -> Node:
        """Pull structured fields out of text with a model::

            fields = wf.extract_parameters(
                start["email"],
                [parameter("order_id", description="the order number"),
                 parameter("urgent", "boolean", description="does it read as urgent")],
                model=MODEL,
            )
            wf.end({"order": fields["order_id"]})

        Each parameter becomes an output under its own name, alongside
        ``__is_success`` and ``__reason``, which is how a failed extraction is
        told from one that found nothing.

        ``reasoning='function_call'`` asks the model to call a tool instead of
        answering in prose; it needs a model that supports tool calls.
        """
        with _node_rules():
            data = models.extractor_data(
                query=reference(query, "query").selector,
                parameters=parameters,
                model=_split_model(model, "model"),
                instruction=instruction,
                reasoning=reasoning,
                title=title,
            )
        return self.add(data, id=id)

    def aggregate(
        self,
        variables: Sequence[Ref],
        *,
        output_type: str = "string",
        title: str = "Variable Aggregator",
        id: str | None = None,
    ) -> Node:
        """Collect whichever of several branches ran into one ``output``.

        Branches that did not run contribute nothing, so this is how two arms
        of an if-else rejoin::

            merged = wf.aggregate([hot.output, cold.output])
            wf.end({"answer": merged.output})
        """
        with _node_rules():
            data = logic.aggregate_data(
                variables=variables, output_type=output_type, title=title
            )
        return self.add(data, id=id)

    def merge(
        self,
        *values: Node | VarRef,
        output_type: str = "string",
        title: str = "Merge",
        id: str | None = None,
    ) -> Node:
        """Rejoin branches: whichever one ran arrives here under one name.

        The node is Dify's variable aggregator, and it is **not optional**
        after a branch. Without it, text that reads from both arms renders the
        arm that did not run as its literal ``{{#…#}}`` reference, because
        that variable was never produced::

            wf.answer(wf.merge(escalate, queue))
        """
        return self.aggregate(
            [value.output if isinstance(value, Node) else value for value in values],
            output_type=output_type,
            title=title,
            id=id,
        )

    def assign(
        self,
        assignments: Sequence[tuple[Ref, str, Any]],
        *,
        title: str = "Variable Assigner",
        id: str | None = None,
    ) -> Node:
        """Write to conversation variables — the only variables that outlive a run::

            history = wf.conversation_var("history", [], type="array[string]")
            wf.assign([(history, "append", answer.output)])

        Each assignment is ``(target, operation, value)``. A ``VarRef`` value
        copies from that variable; anything else is written as a constant.
        Operations are Dify's own: ``over-write``, ``append``, ``extend``,
        ``clear``, ``set``, ``+=``, ``-=``, ``*=``, ``/=``, ``remove-first``,
        ``remove-last``.
        """
        with _node_rules():
            data = logic.assign_data(assignments=assignments, title=title)
        return self.add(data, id=id)

    def extract_text(
        self,
        files: Ref,
        *,
        title: str = "Doc Extractor",
        id: str | None = None,
    ) -> Node:
        """Read the text out of uploaded files, so a model can be given it."""
        data = DocumentExtractorNodeData(
            title=title, variable_selector=reference(files, "files").selector
        )
        return self.add(data, id=id)

    def list_operator(
        self,
        variable: Ref,
        *,
        where: Sequence[FilterCondition] = (),
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        extract: int | None = None,
        title: str = "List Operator",
        id: str | None = None,
    ) -> Node:
        """Filter, sort and cut an array variable.

        ``extract`` takes one element by position, counting from 1, and is
        what Dify's editor calls "extract the Nth item".
        """
        with _node_rules():
            data = logic.list_operator_data(
                variable=reference(variable, "variable").selector,
                where=where,
                order_by=order_by,
                descending=descending,
                limit=limit,
                extract=extract,
                title=title,
            )
        return self.add(data, id=id)

    def human_input(
        self,
        form_content: str,
        *,
        inputs: Sequence[FormInputConfig] = (),
        actions: Sequence[UserActionConfig] = (),
        timeout: int = 36,
        timeout_unit: str = "hour",
        title: str = "Human Input",
        id: str | None = None,
    ) -> Branch:
        """Pause the run until a person answers a form.

        Each action is a branch, handled by its id, and ``timeout`` is a
        branch of its own for when nobody answers::

            from dify_client.workflow import action, form_paragraph

            review = wf.human_input(
                "Approve this draft?\\n\\n{{#$output.note#}}",
                inputs=[form_paragraph("note")],
                actions=[action("approve", "Approve", style="primary"),
                         action("reject", "Reject")],
            )
            wf.connect(review, ship, handle="approve")
            wf.connect(review, stop, handle="reject")

        The form's fields become this node's outputs under their own names.
        Dify runs the waiting, so ``wf.run()`` cannot: deploy it and watch the
        run pause on a real instance.
        """
        with _node_rules():
            data = human_input.form_data(
                form_content=form_content,
                inputs=inputs,
                actions=actions,
                timeout=timeout,
                timeout_unit=timeout_unit,
                title=title,
            )
        return self._add_as(Branch, data, id)

    def agent(
        self,
        strategy: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        label: str = "",
        plugin: str | None = None,
        title: str = "Agent",
        id: str | None = None,
    ) -> Node:
        """An agent node driven by a strategy from a plugin.

        ``strategy`` is ``provider/name`` as the plugin declares it, for
        example ``langgenius/agent/function_calling``. A ``VarRef`` parameter
        is passed as a variable, a string containing ``{{#…#}}`` as a template,
        and anything else as a constant::

            think = wf.agent("langgenius/agent/function_calling",
                             {"query": start["q"]},
                             plugin="langgenius/agent:0.0.18@<hash>")

        Pass ``plugin=`` to have Dify install the plugin on import, the way
        ``wf.tool(...)`` does from a catalog entry.
        """
        with _node_rules():
            data = agents.strategy_data(
                strategy=strategy,
                parameters={
                    key: NodeInput(**_node_input(value))
                    for key, value in (parameters or {}).items()
                },
                label=label,
                title=title,
            )
        node = self.add(data, id=id)
        if plugin:
            self.depends_on(plugin)
        return node

    def datasource(
        self,
        *,
        plugin_id: str,
        provider: str,
        provider_type: str = "local_file",
        name: str = "local_file",
        config: Mapping[str, Any] | None = None,
        parameters: Mapping[str, Any] | None = None,
        plugin_unique_identifier: str | None = None,
        title: str = "Datasource",
        id: str | None = None,
    ) -> Node:
        """Where documents come from, in place of a start node.

        The identifiers come from an installed datasource plugin, so read them
        off the workspace rather than typing them.
        """
        with _node_rules():
            data = knowledge.datasource_data(
                plugin_id=plugin_id,
                provider=provider,
                provider_type=provider_type,
                name=name,
                config=config,
                parameters=(
                    {
                        key: NodeInput(**_node_input(value))
                        for key, value in parameters.items()
                    }
                    if parameters
                    else None
                ),
                plugin_unique_identifier=plugin_unique_identifier,
                title=title,
            )
        return self.add(data, id=id)

    def knowledge_index(
        self,
        chunks: Ref,
        *,
        structure: str = "text_model",
        indexing: str = "economy",
        embedding: str | None = None,
        search: str | None = None,
        top_k: int | None = None,
        score_threshold: float | None = None,
        rerank: str | None = None,
        weights: Mapping[str, Any] | None = None,
        retrieval: Mapping[str, Any] | None = None,
        keyword_number: int | None = None,
        summary: Mapping[str, Any] | None = None,
        title: str = "Knowledge Base",
        id: str | None = None,
    ) -> Node:
        """Write chunks into a knowledge base — the far end of a pipeline.

        ``chunks`` points at the variable holding them, and they are *chunks*:
        Dify validates what arrives as a structured chunk, so text straight
        from an extractor fails indexing after the document is already queued.
        A chunker plugin produces them — ``langgenius/general_chunker`` in
        Dify's own templates, reached through ``wf.tool(...)``. ``structure`` is how
        they are shaped, spelled the way Dify's index processor does:
        ``text_model`` for ordinary chunks, ``hierarchical_model`` for
        parent-child, ``qa_model`` for question-and-answer pairs.

        ``indexing`` is ``economy`` (keyword search, no embedding model) or
        ``high_quality`` (embedded and searched by vector), and ``embedding``
        names the model the second one uses.

        The rest describe how the base will be searched, and become its
        permanent retrieval settings — every later retrieval, a workflow's
        knowledge node included, reads them::

            pipe.knowledge_index(
                chunks.output,
                indexing="high_quality",
                embedding="langgenius/openai/openai:text-embedding-3-small",
                search="hybrid_search",
                rerank="langgenius/cohere/cohere:rerank-v3.5",
            )

        ``rerank`` scores results with a model; ``weights=weighted_score(...)``
        blends vector and keyword scores instead, and only one of the two
        applies. ``retrieval=`` takes the whole block for anything these do not
        cover. Dify validates this node as a knowledge-base configuration and
        refuses the import when the block is missing, so one is always written.
        """
        with _node_rules():
            knowledge.check_index_settings(
                indexing=indexing,
                embedding=embedding,
                retrieval=retrieval,
                search=search,
                rerank=rerank,
                weights=weights,
                top_k=top_k,
                score_threshold=score_threshold,
            )
        settings = (
            dict(retrieval)
            if retrieval is not None
            else _search_settings(
                search=search or knowledge.default_search(indexing),
                top_k=knowledge.INDEX_TOP_K if top_k is None else top_k,
                score_threshold=score_threshold,
                rerank=rerank,
                weights=weights,
            )
        )
        with _node_rules():
            data = knowledge.index_data(
                chunks=reference(chunks, "chunks").selector,
                structure=structure,
                indexing=indexing,
                embedding=(_split_model(embedding, "embedding") if embedding else None),
                settings=settings,
                keyword_number=keyword_number,
                summary=summary,
                title=title,
            )
        return self.add(data, id=id)

    def iteration(
        self,
        items: Ref,
        *,
        parallel: bool = False,
        parallel_nums: int = 10,
        on_error: str = "terminated",
        flatten: bool = True,
        title: str = "Iteration",
        id: str | None = None,
    ) -> Iteration:
        """Run the nodes inside once per element of an array.

        Everything built inside the ``with`` block belongs to the iteration,
        and ``returns()`` names the value each pass contributes::

            with wf.iteration(start["names"]) as each:
                greet = wf.template("Hi {{ n }}", variables={"n": each.item})
                each.returns(greet.output)
            wf.connect(start, each, wf.end({"all": each.output}))

        ``each.item`` is the current element and ``each.index`` its position.
        The node's own ``output`` is the array of what every pass returned.

        ``on_error`` is Dify's own: ``terminated`` stops the run,
        ``continue-on-error`` keeps going, ``remove-abnormal-output`` drops the
        failed pass from the result.
        """
        node_id = self._claim_id(
            id,
            IterationNodeData(
                title=title, start_node_id="", iterator_selector=[], output_selector=[]
            ),
        )
        with _node_rules():
            data = logic.iteration_data(
                node_id=node_id,
                items=reference(items, "items").selector,
                parallel=parallel,
                parallel_nums=parallel_nums,
                on_error=on_error,
                flatten=flatten,
                title=title,
            )
        return self._open_container(Iteration, node_id, data, IterationStartNodeData)

    def loop(
        self,
        *,
        until: Sequence[Condition] = (),
        count: int = 10,
        logical: str = "and",
        variables: Sequence[LoopVariableData] = (),
        title: str = "Loop",
        id: str | None = None,
    ) -> Loop:
        """Run the nodes inside over and over until a condition holds.

        Unlike an iteration, which walks an array, a loop repeats until a
        break condition holds or ``count`` passes have run — whichever comes
        first, because Dify always bounds it::

            with wf.loop(count=5) as body:
                guess = wf.llm("Try again", model=MODEL)
                body.until([when(guess["text"], "contains", "done")])

        ``body.until(...)`` is where a condition about the body goes, since
        the body is built after the loop opens. The ``until=`` argument here
        is for conditions that name something outside it, and the two are
        combined — with ``logical``, which all of a loop's conditions share.

        ``variables=`` declares values that carry from one pass to the next;
        build them with ``loop_var(...)``, and read them as ``body.var(name)``.
        """
        node_id = self._claim_id(
            id,
            LoopNodeData(
                title=title,
                start_node_id="",
                loop_count=count,
                break_conditions=[],
                logical_operator=cast(Any, logical),
            ),
        )
        with _node_rules():
            data = logic.loop_data(
                node_id=node_id,
                until=until,
                count=count,
                logical=logical,
                variables=variables,
                title=title,
            )
        return self._open_container(Loop, node_id, data, LoopStartNodeData)

    def _open_container(
        self,
        container: type[ContainerT],
        node_id: str,
        data: BaseNodeData,
        start_data: type[BaseNodeData],
    ) -> ContainerT:
        """Add a container node and the start marker its body begins at.

        A container built inside another belongs to it like any other node, so
        it is parented the same way: an iteration nested in a loop used to be
        written at the top level, leaving the loop reported as empty.
        """
        node = container(id=node_id, data=data, workflow=self)
        self._nodes.append(node)
        if self._container_stack:
            self._parents[node_id] = self._container_stack[-1]
        # The marker is a node too, so its id is claimed rather than assumed:
        # a node already called `<container>start` would otherwise be written
        # twice under one id.
        start_id = self._claim_free(f"{node_id}start")
        data.start_node_id = start_id  # type: ignore[attr-defined]
        self._nodes.append(Node(id=start_id, data=start_data(title="")))
        self._parents[start_id] = node_id
        self._containers[node_id] = container.kind
        self._container_starts[node_id] = start_id
        return node

    def _close_container(self, container: Container) -> None:
        """Check a container's body and wire its start marker into it."""
        start_id = self._container_starts[container.id]
        children = [
            node.id
            for node in self._nodes
            if self._parents.get(node.id) == container.id and node.id != start_id
        ]
        if not children:
            msg = (
                f"{container.id!r} contains no nodes. Build them inside the "
                "with-block, where they become part of it."
            )
            raise WorkflowError(msg)
        container._check_body()
        self._closed.append(container.id)

    def _start_edges(self, among: Sequence[Edge]) -> list[Edge]:
        """Edges from each container's start marker into its body.

        Every body node that nothing inside the body leads to starts the pass
        — not only the first one built. Two nodes that both read ``each.item``
        run side by side; wiring the marker to the first alone left the
        second with no way in, and each pass returned ``None`` for it.

        Worked out when the edges are read rather than when the block closes,
        so the inferred edges inside the body are already known.
        """
        found: list[Edge] = []
        for container_id in self._closed:
            start_id = self._container_starts[container_id]
            children = [
                node.id
                for node in self._nodes
                if self._parents.get(node.id) == container_id and node.id != start_id
            ]
            inside = set(children) | {start_id}
            entered = {edge.target for edge in among if edge.source in inside}
            found.extend(
                Edge(start_id, child) for child in children if child not in entered
            )
        return found

    def _unreachable_references(self) -> list[tuple[str, str]]:
        """Nodes that read a node no path leads from, as ``(reader, source)``.

        Wiring one node by hand switches inference off for it, and that is the
        rule — but it also means an explicit edge can *replace* the edge a
        reference needed rather than adding to it. A chunker connected
        straight to its datasource reads an extractor that now has no path to
        it: the document imports, publishes, and runs the chunker on nothing.

        A reference into a container's body is not a path question — the body
        runs when the container does — so reaching the container counts as
        reaching what it holds.
        """
        edges = self.edges
        forward: dict[str, set[str]] = {}
        for edge in edges:
            forward.setdefault(edge.source, set()).add(edge.target)

        def reaches(source: str, target: str) -> bool:
            wanted = {target, *self._ancestors(target)}
            seen, stack = set(), [source]
            while stack:
                current = stack.pop()
                if current in wanted:
                    return True
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(forward.get(current, ()))
            return False

        dangling: list[tuple[str, str]] = []
        for node in self._nodes:
            held = {
                other.id
                for other in self._nodes
                if node.id in self._ancestors(other.id)
            }
            for source in sorted(self._referenced(node)):
                # A container reads its own body — that is what `returns()`
                # is — and the body runs within it rather than before it.
                if source in held:
                    continue
                if not reaches(source, node.id):
                    dangling.append((node.id, source))
        return dangling

    def _ancestors(self, node_id: str) -> set[str]:
        """Every container this node sits inside, outermost last."""
        found: set[str] = set()
        parent = self._parents.get(node_id)
        while parent is not None:
            found.add(parent)
            parent = self._parents.get(parent)
        return found

    def _check_references_are_reachable(self) -> None:
        """Raise when a node reads something that cannot have run yet."""
        dangling = self._unreachable_references()
        if not dangling:
            return
        by_id = {node.id: node for node in self._nodes}
        for reader, source in dangling:
            branch = by_id.get(source)
            if _is_branch(branch):
                arms = ", ".join(repr(arm) for arm in branch.handles)  # type: ignore[union-attr]
                msg = (
                    f"{reader} reads {source}, which is a branch, and nothing "
                    f"says which of its arms leads to {reader}. Connect one: "
                    f"wf.connect({source}.case(...), {reader}) — its arms are "
                    f"{arms}."
                )
                raise WorkflowError(msg)
        listed = ", ".join(f"{reader} reads {source}" for reader, source in dangling)
        msg = (
            f"These nodes read a node nothing leads to: {listed}. An edge "
            "written by hand replaces the one the reference would have "
            "derived, so connecting past a node leaves it out of the run."
        )
        raise WorkflowError(msg)

    def _nested_positions(self) -> dict[str, dict[str, int]]:
        """Lay a container's body out inside it, left to right.

        Positions of nested nodes are relative to their parent, which is what
        React Flow means by ``parentId``; an absolute position here would put
        the body somewhere off the canvas.
        """
        placed: dict[str, dict[str, int]] = {}
        for container_id in self._containers:
            body = [
                node.id
                for node in self._nodes
                if self._parents.get(node.id) == container_id
            ]
            start_id = self._container_starts[container_id]
            column = 0
            for node_id in body:
                if node_id == start_id:
                    placed[node_id] = {"x": 24, "y": 68}
                    continue
                placed[node_id] = {"x": 140 + column * COLUMN_WIDTH, "y": 68}
                column += 1
        return placed

    def _node_dsl(self, node: Node, position: dict[str, int]) -> dict[str, Any]:
        """One graph node, with the nesting fields Dify's canvas needs."""
        data = node.data.model_dump(mode="json", exclude_none=True)
        entry: dict[str, Any] = {
            "id": node.id,
            "type": _CANVAS_TYPES.get(node.type, "custom"),
            "position": position,
            "data": data,
        }
        parent = self._parents.get(node.id)
        if parent is not None:
            kind = self._containers[parent]
            entry["parentId"] = parent
            entry["extent"] = "parent"
            entry["zIndex"] = NESTED_Z_INDEX
            if kind == "iteration":
                data["isInIteration"] = True
                data["iteration_id"] = parent
            else:
                data["isInLoop"] = True
                data["loop_id"] = parent
        if node.id in self._containers:
            # The editor sizes a container from the nodes it holds; a
            # generated one has no rendered children to measure, so it says
            # how big it is and the canvas honours it.
            held = sum(
                1 for other in self._nodes if self._parents.get(other.id) == node.id
            )
            entry["width"] = 180 + held * COLUMN_WIDTH
            entry["height"] = CONTAINER_HEIGHT
        return entry

    def _shared_parent(self, edge: Edge) -> tuple[str, str] | None:
        """The container both ends of an edge sit in, if they sit in one."""
        source = self._parents.get(edge.source)
        if source is None or source != self._parents.get(edge.target):
            return None
        return source, self._containers[source]

    def _workflow_section(self, *, include_secret: bool) -> dict[str, Any]:
        """The ``workflow`` half of a DSL document: the graph and its variables.

        Shared with :class:`~dify_client.workflow.pipeline.Pipeline`, whose
        document differs only in its envelope.
        """
        edges = self.edges
        top_level = [n.id for n in self._nodes if n.id not in self._parents]
        positions = assign_positions(
            top_level,
            [
                (e.source, e.target)
                for e in edges
                if e.source in set(top_level) and e.target in set(top_level)
            ],
        )
        positions.update(self._nested_positions())
        types = {n.id: n.type for n in self._nodes}
        return {
            "graph": {
                "nodes": [
                    self._node_dsl(node, positions[node.id]) for node in self._nodes
                ],
                "edges": [
                    {
                        "id": f"{edge.source}-{edge.source_handle}-{edge.target}",
                        "source": edge.source,
                        "target": edge.target,
                        "sourceHandle": edge.source_handle,
                        "targetHandle": "target",
                        "type": "custom",
                        "data": _edge_data(
                            source_type=types[edge.source],
                            target_type=types[edge.target],
                            container=self._shared_parent(edge),
                        ),
                    }
                    for edge in edges
                ],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
            "features": {},
            "environment_variables": [
                var.to_dsl(include_secret=include_secret) for var in self._env_vars
            ],
            "conversation_variables": [var.to_dsl() for var in self._conversation_vars],
        }

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

    def _claim_id(self, requested: str | None, data: BaseNodeData) -> str:
        """Take an id, refusing to reuse one the caller asked for by name."""
        if requested is not None and requested in self._ids:
            msg = f"Node id {requested!r} is already used in this workflow."
            raise WorkflowError(msg)
        return self._claim_free(requested or str(data.type).replace("-", "_"))

    def _claim_free(self, base: str) -> str:
        """Take ``base``, or the first free name derived from it.

        For ids this SDK chooses rather than the caller — a node named after
        its type, a container's start marker — a collision is a numbering
        question, not a mistake to report.
        """
        node_id = base
        counter = 2
        while node_id in self._ids:
            node_id = f"{base}_{counter}"
            counter += 1
        self._ids.add(node_id)
        return node_id


def load_yaml(source: str | Path) -> dict[str, Any]:
    """Read a Dify DSL document from a path or a YAML string."""
    path = Path(source) if isinstance(source, (str, Path)) else None
    if path is not None and path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return yaml.safe_load(str(source))
