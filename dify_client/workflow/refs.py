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
    """

    node_id: str
    field: str

    @property
    def selector(self) -> list[str]:
        """The reference as a DSL value selector."""
        return [self.node_id, self.field]

    @property
    def template(self) -> str:
        """The reference as a DSL template placeholder."""
        return f"{{{{#{self.node_id}.{self.field}#}}}}"

    def __str__(self) -> str:
        return self.template


#: Anything accepted where a piece of text may embed variable references.
Text = str | VarRef


def render(value: Text) -> str:
    """Render a template string or a variable reference to DSL text."""
    return value.template if isinstance(value, VarRef) else value


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
        return VarRef(self.id, field)

    @property
    def output(self) -> VarRef:
        """Reference to this node's default output field."""
        return VarRef(self.id, _DEFAULT_OUTPUT_FIELD.get(self.type, "output"))

    def __str__(self) -> str:
        return self.id


# Nodes whose conventional single output is not called "output".
_DEFAULT_OUTPUT_FIELD: dict[str, str] = {
    "llm": "text",
    "tool": "text",
    "http-request": "body",
    "document-extractor": "text",
    "question-classifier": "class_name",
}


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


def as_dsl(value: Any) -> Any:
    """Normalise a builder argument into plain DSL data."""
    if isinstance(value, VarRef):
        return value.template
    if isinstance(value, Node):
        return value.id
    return value
