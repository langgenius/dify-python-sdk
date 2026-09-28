"""The pieces a workflow graph is made of, and the rules each one carries.

Values rather than machinery: an environment variable, an edge, a container,
the arguments a node takes, the credentials an HTTP node uses. They are here
because :mod:`dify_client.workflow.graph` is about assembling a graph and this
is about what it assembles — and because both documents, an app and a
knowledge pipeline, are assembled from the same pieces.

Each carries the one rule Dify would otherwise only report much later: a
duplicate name, an operator Dify does not have, a model reference with no
model in it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, TypeVar
from uuid import uuid4

from graphon.nodes.loop.entities import LoopEndNodeData

from ..search import retrieval_model, split_model
from .nodes import MultipleRetrievalConfig
from .nodes._errors import NodeError
from .refs import Node, Ref, VarRef, reference

__all__ = [
    "CONTAINER_HEIGHT",
    "NESTED_Z_INDEX",
    "ROOT_NODE_TYPES",
    "Container",
    "Edge",
    "EnvVar",
    "Iteration",
    "Loop",
    "WorkflowError",
]


ROOT_NODE_TYPES = frozenset(
    {"start", "datasource", "trigger-webhook", "trigger-schedule", "trigger-plugin"}
)
ContainerT = TypeVar("ContainerT", bound="Container")
NodeT = TypeVar("NodeT", bound=Node)


class WorkflowError(Exception):
    """Raised when a workflow is built in a way Dify would reject."""


class _WrongContainer(WorkflowError, AttributeError):
    """A member of the other kind of container, asked of this one."""


@contextmanager
def _node_rules() -> Iterator[None]:
    """Speak a node module's refusals in the document's own vocabulary.

    A node knows what Dify accepts; it does not know whether it is being added
    to a workflow or a pipeline. It raises ``NodeError``; callers catch
    ``WorkflowError``, and the message is the same either way.
    """
    try:
        yield
    except NodeError as error:
        raise WorkflowError(str(error)) from error


def _claim_name(
    existing: Iterable[str], name: str, what: str, *, where: str = ""
) -> None:
    """Refuse a second declaration of the same name.

    A duplicate is accepted by the DSL and only one of the two survives in
    Dify, so it is caught where it is written.
    """
    if name in set(existing):
        scope = f" on {where!r}" if where else ""
        msg = f"{what} {name!r} is already declared{scope}."
        raise WorkflowError(msg)


def _search_settings(**settings: Any) -> dict[str, Any]:
    """:func:`dify_client.search.retrieval_model`, refusing in this module's words."""
    try:
        return retrieval_model(**settings)
    except ValueError as error:
        raise WorkflowError(str(error)) from error


def _split_model(model: str, what: str) -> tuple[str, str]:
    """Split a model reference, reporting it in this module's own error.

    The parsing itself belongs to :mod:`dify_client.search`, which the
    knowledge client uses too; only the exception differs, because a caller
    building a workflow is told about workflows.
    """
    try:
        return split_model(model, what)
    except ValueError as error:
        raise WorkflowError(str(error)) from error


_ASSIGN_OPERATIONS = frozenset(
    {
        "over-write",
        "clear",
        "append",
        "extend",
        "set",
        "+=",
        "-=",
        "*=",
        "/=",
        "remove-first",
        "remove-last",
    }
)


#: The fields a knowledge node carries out of a knowledge base's search
#: settings. It searches bases that already have their own, so it overrides
#: only these — not the search method, which is the base's, and not the
#: threshold's enable flag, which this node does not have.
_NODE_RETRIEVAL_FIELDS = (
    "top_k",
    "score_threshold",
    "reranking_mode",
    "reranking_enable",
    "reranking_model",
    "weights",
)


def _node_retrieval(
    *,
    top_k: int,
    score_threshold: float | None,
    rerank: str | None,
    weights: Mapping[str, Any] | None,
) -> MultipleRetrievalConfig:
    """Build the knowledge node's retrieval config from the shared settings.

    ``retrieval_model`` decides what reranking means — a model, or a weighted
    score, never both — and this keeps the node from deciding it a second time.
    The node's own model then validates the result, which is what makes the two
    shapes one shape.
    """
    settings = _search_settings(
        top_k=top_k,
        score_threshold=score_threshold,
        rerank=rerank,
        weights=weights,
    )
    if weights is not None:
        # On a knowledge base the flag is left off and hybrid search reads the
        # weights anyway. On this node it is the switch: Dify merges several
        # bases with the weights only `if reranking_enable and dataset_count >
        # 1` (core/rag/retrieval/dataset_retrieval.py), so off meant weights=
        # was written and never applied. Dify's own editor writes it off too.
        settings["reranking_enable"] = True
    return MultipleRetrievalConfig.model_validate(
        {key: settings[key] for key in _NODE_RETRIEVAL_FIELDS}
    )


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


#: Node types the canvas draws with a renderer of their own rather than the
#: ordinary node box.
_CANVAS_TYPES = {
    "iteration-start": "custom-iteration-start",
    "loop-start": "custom-loop-start",
}

#: React Flow elevates a selected node by 1000, so nested elements sit above.
NESTED_Z_INDEX = 1001

#: How tall a generated container is drawn.
CONTAINER_HEIGHT = 250


def _edge_data(
    *,
    source_type: str,
    target_type: str,
    container: tuple[str, str] | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "sourceType": source_type,
        "targetType": target_type,
        "isInIteration": False,
        "isInLoop": False,
    }
    if container is not None:
        container_id, kind = container
        if kind == "iteration":
            data["isInIteration"] = True
            data["iteration_id"] = container_id
        else:
            data["isInLoop"] = True
            data["loop_id"] = container_id
    return data


_OTHER_CONTAINER_MEMBERS = {
    "item": ("an iteration", "a loop repeats without one"),
    "index": ("an iteration", "a loop repeats without one"),
    "returns": ("an iteration", "a loop writes to a loop variable instead"),
    "var": ("a loop", "an iteration reads its element with item"),
    "stop": ("a loop", "an iteration ends when its array does"),
}


@dataclass(frozen=True)
class Container(Node):
    """A node that runs other nodes: an iteration or a loop.

    Used as a context manager: every node built inside the block belongs to
    the container rather than to the workflow around it, which is what the
    ``parentId`` in the DSL says and what makes Dify draw them inside the box.

    The two kinds read differently — an iteration walks an array, a loop
    repeats until told to stop — so each has its own subclass rather than a
    flag and a pile of guards.
    """

    workflow: Any

    #: The DSL's word for this kind of container.
    kind: ClassVar[str] = ""

    def __enter__(self) -> Container:
        self.workflow._container_stack.append(self.id)
        return self

    def __exit__(self, exc_type: object, *_rest: object) -> None:
        self.workflow._container_stack.pop()
        # A body that raised is not a body to check: reporting that the
        # container is empty would bury the error that emptied it.
        if exc_type is None:
            self.workflow._close_container(self)

    def _check_body(self) -> None:
        """Raise if the container is missing something only it can require."""

    def __getattr__(self, name: str) -> Any:
        # Only reached when the attribute is genuinely absent, which for these
        # five names means the other kind of container was expected. The error
        # is an AttributeError too: a plain WorkflowError broke the protocol,
        # so hasattr(loop, "item") raised instead of answering False.
        if name in _OTHER_CONTAINER_MEMBERS:
            owner, instead = _OTHER_CONTAINER_MEMBERS[name]
            msg = f"Only {owner} has {name}; {instead}."
            raise _WrongContainer(msg)
        raise AttributeError(name)


@dataclass(frozen=True)
class Iteration(Container):
    """Runs its body once per element of an array."""

    kind: ClassVar[str] = "iteration"

    @property
    def item(self) -> VarRef:
        """The element this pass is working on."""
        return VarRef(self.id, "item")

    @property
    def index(self) -> VarRef:
        """How far through the array this pass is, from zero."""
        return VarRef(self.id, "index")

    def returns(self, value: Ref) -> None:
        """Name what each pass contributes to the iteration's output array."""
        selector = reference(value, "returns()").selector
        self.data.output_selector = selector  # type: ignore[attr-defined]

    def _check_body(self) -> None:
        if not getattr(self.data, "output_selector", None):
            msg = (
                f"{self.id!r} never says what each pass returns. "
                "Call returns(node.output) inside the with-block."
            )
            raise WorkflowError(msg)


@dataclass(frozen=True)
class Loop(Container):
    """Runs its body over and over until a condition holds or a count runs out."""

    kind: ClassVar[str] = "loop"

    def var(self, name: str) -> VarRef:
        """A loop variable, by the label it was declared with.

        Loop variables live on the loop node itself, which is what lets a pass
        read what the previous one wrote.
        """
        declared = {v.label for v in self.data.loop_variables}  # type: ignore[attr-defined]
        if name not in declared:
            known = ", ".join(sorted(declared)) or "none"
            msg = f"{name!r} is not a variable of this loop. Declared: {known}."
            raise WorkflowError(msg)
        return VarRef(self.id, name)

    def until(self, conditions: Sequence[Any], *, logical: str | None = None) -> None:
        """Stop after the pass in which these hold.

        Added to what ``wf.loop(until=[...])`` was given, from inside the
        block — which is the only place a condition can name a node in the
        body, since those are built after the loop opens::

            with wf.loop(count=5, until=[when(start["mode"], "is", "once")]) as body:
                guess = wf.llm("Try again", model=MODEL)
                body.until([when(guess["text"], "contains", "done")])

        All of a loop's conditions share one ``logical`` operator, the loop's
        own unless this is the first; asking for a different one once there
        are conditions is refused, since it would change what the earlier
        ones mean. Dify checks them between passes, so the pass that
        satisfies them still finishes. ``count`` bounds the loop whatever they
        say.
        """
        data: Any = self.data
        existing = list(data.break_conditions or [])
        if logical is not None and existing and logical != data.logical_operator:
            msg = (
                f"{self.id!r} already combines its conditions with "
                f"{data.logical_operator!r}, and a loop has one operator for "
                f"all of them. Pass logical={data.logical_operator!r}, or set "
                "it once on wf.loop(logical=...)."
            )
            raise WorkflowError(msg)
        # Replacing them dropped the ones wf.loop(until=...) was given, with
        # nothing said.
        data.break_conditions = [*existing, *conditions]
        if logical is not None:
            data.logical_operator = logical

    def stop(self, *, title: str = "Exit Loop", id: str | None = None) -> Node:
        """A node that leaves the loop as soon as the run reaches it.

        The counterpart of ``until``: a condition is checked between passes,
        while this ends the loop part-way through one::

            with wf.loop(count=10) as body:
                found = wf.llm("Search again", model=MODEL)
                check = wf.if_else([when(found["text"], "contains", "yes")])
                wf.connect(check, body.stop(), handle="true")
        """
        return self.workflow.add(LoopEndNodeData(title=title), id=id)


class Edge:
    """A connection between two nodes."""

    __slots__ = ("source", "target", "source_handle")

    def __init__(self, source: str, target: str, source_handle: str = "source"):
        self.source = source
        self.target = target
        self.source_handle = source_handle


def _node_input(value: Any) -> dict[str, Any]:
    """Classify an argument the way Dify's editor does.

    One shape, four node types: a tool's parameters, an agent strategy's, a
    datasource's and a plugin trigger's are all ``{"type": …, "value": …}``
    with the same three kinds.
    """
    if isinstance(value, VarRef):
        return {"type": "variable", "value": value.selector}
    if isinstance(value, Node):
        return {"type": "variable", "value": value.output.selector}
    if isinstance(value, str) and "{{#" in value:
        return {"type": "mixed", "value": value}
    return {"type": "constant", "value": value}
