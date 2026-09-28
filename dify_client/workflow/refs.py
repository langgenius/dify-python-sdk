"""Variable references and node handles used when building a workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from graphon.entities.base_node_data import BaseNodeData


@dataclass(frozen=True)
class VarRef:
    """A reference to one output field of a node.

    Dify expresses the same reference two ways: as a ``{{#node.field#}}``
    template inside prompts and answers, and as a ``["node", "field"]``
    selector inside node configuration. A ``VarRef`` carries both so callers
    never hand-write either form.

    ``sub`` is the third segment a knowledge pipeline's inputs need: they live
    under ``rag``, keyed by the datasource node that asks for them, so the
    reference is ``["rag", "<node>", "<variable>"]``.
    """

    node_id: str
    field: str
    sub: str | None = None

    @property
    def parts(self) -> tuple[str, ...]:
        """Every segment of the reference, in order."""
        return (
            (self.node_id, self.field)
            if self.sub is None
            else (
                self.node_id,
                self.field,
                self.sub,
            )
        )

    @property
    def selector(self) -> list[str]:
        """The reference as a DSL value selector."""
        return list(self.parts)

    @property
    def template(self) -> str:
        """The reference as a DSL template placeholder."""
        return f"{{{{#{'.'.join(self.parts)}#}}}}"

    def __str__(self) -> str:
        return self.template


def render(value: Any) -> str:
    """Render text, a variable reference or a node to DSL text."""
    if isinstance(value, Node):
        return value.output.template
    if isinstance(value, VarRef):
        return value.template
    return value


@dataclass(frozen=True)
class Node:
    """A node that has been added to a workflow.

    Indexing a node produces a reference to one of its output fields::

        llm["text"]     # -> {{#llm.text#}}

    ``output`` is shorthand for the conventional single output field of
    nodes that only produce one value.
    """

    id: str
    data: BaseNodeData

    @property
    def type(self) -> str:
        """The DSL node type, e.g. ``llm`` or ``template-transform``."""
        return str(self.data.type)

    @property
    def title(self) -> str:
        return self.data.title

    def __getitem__(self, field: str) -> VarRef:
        # Python falls back to __getitem__(0), __getitem__(1), … when it
        # iterates an object with no __iter__. Answering those would make a
        # node an endless sequence — `list(node)` never stopped — so a field
        # that is not a name is refused here.
        if not isinstance(field, str):
            msg = (
                f"A node is indexed by output name, not by position: "
                f"{self.id!r}[{field!r}]. Use node['text'], or node.output "
                "for its conventional one."
            )
            raise TypeError(msg)
        return VarRef(self.id, field)

    @property
    def output(self) -> VarRef:
        """Reference to this node's default output field."""
        return VarRef(self.id, _DEFAULT_OUTPUT_FIELD.get(self.type, "output"))

    def __str__(self) -> str:
        # A node stands for its own output everywhere else a node is accepted
        # as text, so it has to stand for it here too: an f-string is the one
        # place the SDK cannot intercept, and a node that rendered as its id
        # put the word "llm" into the answer a user reads.
        return self.output.template


# Nodes whose conventional single output is not called "output".
_DEFAULT_OUTPUT_FIELD: dict[str, str] = {
    "llm": "text",
    "tool": "text",
    "http-request": "body",
    "document-extractor": "text",
    "question-classifier": "class_name",
    "knowledge-retrieval": "result",
    "list-operator": "result",
}


#: Anything accepted where a piece of text may embed variable references. A
#: node stands for its own conventional output, so the common case reads as
#: ``wf.answer(reply)`` rather than ``wf.answer(reply.output)``.
Text = str | VarRef | Node

#: Anything accepted where one value is named: a field of a node, or a node
#: standing for its conventional output. The same rule as ``Text``, for the
#: arguments that take a selector rather than a string.
Ref = VarRef | Node


def reference(value: Any, what: str) -> VarRef:
    """The reference ``value`` names, whichever of the two ways it was written.

    ``Text`` accepted a bare node and the selector-taking arguments did not,
    so ``wf.end({"answer": reply})`` — the line written right after ``reply =
    wf.llm(...)`` — died with an ``AttributeError`` naming neither the
    argument nor the fix.
    """
    if isinstance(value, VarRef):
        return value
    if isinstance(value, Node):
        return value.output
    msg = (
        f"{what} takes a reference — node['field'], or a node for its own "
        f"output — not {type(value).__name__} {value!r}."
    )
    raise TypeError(msg)


def is_reference(value: Any) -> bool:
    """Whether ``value`` names a variable rather than being a constant."""
    return isinstance(value, (VarRef, Node))


@dataclass(frozen=True)
class Handle:
    """One outgoing branch of a node, named the way Dify names it.

    Connecting *from* a handle is how a branch is wired: ``wf.connect(
    branch.true, escalate)``. The alternative is typing the handle's id as a
    string, which is what it was before — and a typo there produces a workflow
    that imports, publishes and quietly never takes that arm.
    """

    node: Node
    name: str

    @property
    def id(self) -> str:
        """The node this branch leaves from."""
        return self.node.id

    def __str__(self) -> str:
        return f"{self.node.id}:{self.name}"


#: The arm a human-input node takes when nobody answers in time, as Dify's
#: engine and editor both spell it (``TIMEOUT_HANDLE`` in
#: ``core/workflow/nodes/human_input/constants.py``). Writing ``"timeout"``
#: produced an edge the engine never takes.
TIMEOUT_HANDLE = "__timeout"


@dataclass(frozen=True)
class Branch(Node):
    """A node that continues along one of several named arms.

    An if-else, a question classifier and a human-input form are all this: the
    node decides, and the decision is an edge handle rather than a value.
    """

    def case(self, name: str) -> Handle:
        """The arm named ``name`` — a case id, a class id, an action id."""
        known = self.handles
        if known and name not in known:
            listed = ", ".join(known)
            hint = (
                f" Dify names the timeout arm {TIMEOUT_HANDLE!r}; use .timeout."
                if name == "timeout" and TIMEOUT_HANDLE in known
                else ""
            )
            msg = f"{name!r} is not an arm of {self.id!r}. It has: {listed}.{hint}"
            raise KeyError(msg)
        return Handle(self, name)

    @property
    def handles(self) -> tuple[str, ...]:
        """Every arm this node can take, in the order it declares them."""
        data = self.data
        cases = getattr(data, "cases", None)
        if cases:
            return tuple(case.case_id for case in cases) + ("false",)
        classes = getattr(data, "classes", None)
        if classes:
            return tuple(item.id for item in classes)
        actions = getattr(data, "user_actions", None)
        if actions:
            return tuple(action.id for action in actions) + (TIMEOUT_HANDLE,)
        return ()

    @property
    def true(self) -> Handle:
        """The arm taken when the condition holds. If-else only."""
        return self.case("true")

    @property
    def false(self) -> Handle:
        """The arm taken when no case matched."""
        return self.case("false")

    @property
    def timeout(self) -> Handle:
        """The arm a human-input form takes when nobody answers in time."""
        return self.case(TIMEOUT_HANDLE)


class SystemVariables:
    """Dify's system variables, referenced as ``sys.*`` inside a workflow.

    A chatflow's incoming message arrives as ``sys.query``, and there is no
    node to index for it, so reach for this instead of hand-building a
    reference::

        wf.template("You said {{ q }}", variables={"q": system.query})

    Any name works — ``system.whatever`` — because the set differs by app mode
    and Dify version; the common ones are ``query``, ``files``, ``user_id``,
    ``conversation_id``, ``dialogue_count``, ``app_id`` and ``workflow_id``.
    """

    __slots__ = ()

    #: The namespace Dify uses for these.
    namespace = "sys"

    def __getattr__(self, name: str) -> VarRef:
        if name.startswith("_"):
            raise AttributeError(name)
        return VarRef(self.namespace, name)

    def __getitem__(self, name: str) -> VarRef:
        return VarRef(self.namespace, name)

    def __repr__(self) -> str:
        return "system"


#: Dify's system variables: ``system.query``, ``system.user_id``, and so on.
system = SystemVariables()
