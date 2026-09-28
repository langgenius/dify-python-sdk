"""Knowledge pipelines: documents in, indexed chunks out.

A pipeline is a workflow with a different envelope and a different job. It
starts at a **datasource** rather than a start node, ends at a
**knowledge-index** node rather than an end or answer node, and produces a
knowledge base rather than a run — which is why Dify serves it from
``/rag/pipelines`` and writes ``kind: rag_pipeline`` rather than ``kind: app``.
Everything between those two ends is the workflow vocabulary: extractors,
chunkers, code, branches.

The graph is built exactly like a workflow, because it is one::

    pipe = Pipeline("support-docs")
    files = pipe.datasource(plugin_id="langgenius/file", provider="file")
    extract = pipe.tool(catalog["dify_extractor"]["dify_extractor"],
                        params={"file": files["file"]})
    chunks = pipe.tool(catalog["general_chunker"]["general_chunker"],
                       params={"content": extract["text"]})
    index = pipe.knowledge_index(chunks["result"])
    pipe.connect(files, extract, chunks, index)

    console.pipelines.deploy(pipe)      # creates the knowledge base

**The knowledge-index node takes chunks, not text.** Dify validates what
reaches it as a structured chunk and a plain string fails *indexing* — after
the document is queued, so the run looks fine and the document lands in
`error`. Chunks come from a chunker plugin, which is a tool node:
``langgenius/general_chunker`` in Dify's own templates, usually after
``langgenius/dify_extractor``. Both come from the marketplace, so a workspace
without them cannot index through a pipeline at all.

Two things differ and both are easy to get wrong.

**The DSL version is its own.** A pipeline document is DSL ``0.1.0``, not the
``0.7.0`` an app uses. Sending an app's version makes Dify hold the import for
confirmation, which is a second round trip and an easy thing to mistake for a
failure.

**A pipeline's inputs live under ``rag``**, keyed by the datasource node that
asks for them — ``{{#rag.<node>.<name>#}}``, a three-part reference. They are
declared with :meth:`Pipeline.variable`, which is also what makes them a form
in Dify's UI.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .graph import GraphDocument
from .parts import ROOT_NODE_TYPES, WorkflowError, _claim_name
from .refs import Node, VarRef

__all__ = ["PIPELINE_DSL_VERSION", "SHARED", "Pipeline", "PipelineVariable"]

#: The DSL version Dify's pipeline importer is on. Not the app one.
PIPELINE_DSL_VERSION = "0.1.0"

#: The namespace a pipeline's own inputs live under.
RAG_NAMESPACE = "rag"

#: The scope of an input that belongs to no single datasource. Dify's own
#: templates use it for the chunking settings every source shares.
SHARED = "shared"

#: Input widgets a pipeline variable may be, as Dify's editor names them.
VARIABLE_TYPES = (
    "text-input",
    "paragraph",
    "select",
    "number",
    "checkbox",
    "file",
    "file-list",
)


class PipelineVariable:
    """One input a pipeline asks for before it runs.

    It belongs to a datasource node — Dify shows the form for the datasource
    being used — and is referenced as ``{{#rag.<node>.<name>#}}``.
    """

    __slots__ = (
        "name",
        "node_id",
        "label",
        "type",
        "required",
        "default",
        "options",
        "max_length",
        "placeholder",
        "tooltip",
    )

    def __init__(
        self,
        name: str,
        node_id: str,
        *,
        label: str = "",
        type: str = "text-input",
        required: bool = True,
        default: Any = None,
        options: Sequence[str] = (),
        max_length: int | None = None,
        placeholder: str | None = None,
        tooltip: str | None = None,
    ):
        self.name = name
        self.node_id = node_id
        self.label = label or name
        self.type = type
        self.required = required
        self.default = default
        self.options = list(options)
        self.max_length = max_length
        self.placeholder = placeholder
        self.tooltip = tooltip

    @property
    def ref(self) -> VarRef:
        """How the datasource refers to this input."""
        return VarRef(RAG_NAMESPACE, self.node_id, self.name)

    def to_dsl(self) -> dict[str, Any]:
        return {
            "belong_to_node_id": self.node_id,
            "variable": self.name,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "default_value": self.default,
            "options": self.options,
            "max_length": self.max_length,
            "placeholder": self.placeholder,
            "tooltips": self.tooltip,
        }

    def __repr__(self) -> str:
        return f"PipelineVariable({self.name!r}, node={self.node_id!r}, type={self.type!r})"


class Pipeline(GraphDocument):
    """A Dify knowledge pipeline defined in code.

    Built with the same node helpers an app has — the graph between the
    datasource and the knowledge base is ordinary workflow work — but not the
    app's ends: there is no start node, no answer, no trigger and no
    conversation variable on a pipeline, because Dify has nowhere to put them.
    What it adds is the envelope, the version and its own inputs; see the
    module docstring.
    """

    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        icon: str = "\U0001f4d9",
        icon_background: str = "#FFEAD5",
    ):
        super().__init__(
            name,
            description=description,
            icon=icon,
            icon_background=icon_background,
        )
        self._variables: list[PipelineVariable] = []

    def variable(
        self,
        node: Node | str | None,
        name: str,
        *,
        label: str = "",
        type: str = "text-input",
        required: bool = True,
        default: Any = None,
        options: Sequence[str] = (),
        max_length: int | None = None,
        placeholder: str | None = None,
        tooltip: str | None = None,
    ) -> VarRef:
        """Declare an input the pipeline asks for, and return its reference::

            crawl = pipe.datasource(plugin_id="langgenius/jina_datasource",
                                    provider="jinareader",
                                    provider_type="website_crawl",
                                    name="jina_reader")
            url = pipe.variable(crawl, "url", label="URL")
            crawl.data.datasource_parameters["url"] = {"type": "mixed",
                                                       "value": str(url)}

        An input usually belongs to a datasource, because that is how Dify
        shows it: the form follows whichever source is being used. Pass
        ``None`` for one that belongs to all of them — Dify calls that scope
        ``shared`` and its own templates keep the chunking settings there::

            size = pipe.variable(None, "chunk_size", type="number", default=500)
            # {{#rag.shared.chunk_size#}}
        """
        node_id = (
            SHARED if node is None else (node if isinstance(node, str) else node.id)
        )
        if type not in VARIABLE_TYPES:
            accepted = ", ".join(VARIABLE_TYPES)
            msg = f"type={type!r} is not an input Dify draws. Use one of: {accepted}."
            raise WorkflowError(msg)
        _claim_name(
            (v.name for v in self._variables if v.node_id == node_id),
            name,
            "Pipeline variable",
            where=node_id,
        )
        variable = PipelineVariable(
            name,
            node_id,
            label=label,
            type=type,
            required=required,
            default=default,
            options=options,
            max_length=max_length,
            placeholder=placeholder,
            tooltip=tooltip,
        )
        self._variables.append(variable)
        return variable.ref

    @property
    def variables(self) -> list[PipelineVariable]:
        return list(self._variables)

    def validate(self) -> None:
        """Check what Dify requires of a pipeline, which is not what it requires
        of an app: a datasource at the front, a knowledge base at the back."""
        if not self._nodes:
            msg = "Pipeline has no nodes."
            raise WorkflowError(msg)
        if not any(n.type == "datasource" for n in self._nodes):
            msg = (
                "A pipeline starts at a datasource. Add one with "
                "pipe.datasource(plugin_id=..., provider=...)."
            )
            raise WorkflowError(msg)
        if not any(n.type == "knowledge-index" for n in self._nodes):
            msg = (
                "A pipeline ends at a knowledge base. Add one with "
                "pipe.knowledge_index(chunks)."
            )
            raise WorkflowError(msg)
        roots = [n for n in self._nodes if n.type in ROOT_NODE_TYPES]
        stray = [n.id for n in roots if n.type != "datasource"]
        if stray:
            msg = (
                f"A pipeline is started by its datasource, so {', '.join(stray)} "
                "does not belong in one."
            )
            raise WorkflowError(msg)
        edges = self.edges
        connected = {e.source for e in edges} | {e.target for e in edges}
        orphans = [n.id for n in self._nodes if n.id not in connected]
        if orphans and len(self._nodes) > 1:
            msg = (
                f"These nodes are not connected to anything: {', '.join(orphans)}. "
                "A node that reads another node's output is connected by that "
                f"alone; one that reads nothing needs pipe.connect(...)."
            )
            raise WorkflowError(msg)
        # A read of a branch names the arm to choose, which says more than
        # "nothing leads here" about the same node, so it is asked first.
        self._check_references_are_reachable()
        self._check_every_node_is_entered()
        # An input belongs to the datasource whose form shows it, or to the
        # `shared` scope. Dify fills in the one being used and nothing else,
        # so a variable owned by a processing node is read as unset at run
        # time rather than refused at import.
        owners = {n.id for n in self._nodes if n.type == "datasource"} | {SHARED}
        known = {n.id for n in self._nodes}
        for variable in self._variables:
            if variable.node_id in owners:
                continue
            where = (
                f"{variable.node_id!r}, which is not a datasource"
                if variable.node_id in known
                else f"{variable.node_id!r}, which is not in the pipeline"
            )
            msg = (
                f"Pipeline variable {variable.name!r} belongs to {where}. Dify "
                "fills in the inputs of the datasource being used, so give it "
                "to one of those or to the shared scope with "
                "pipe.variable(None, ...)."
            )
            raise WorkflowError(msg)

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        """Render the pipeline as a Dify ``rag_pipeline`` DSL document."""
        self.validate()
        workflow = self._workflow_section(include_secret=include_secret)
        workflow["rag_pipeline_variables"] = [v.to_dsl() for v in self._variables]
        document: dict[str, Any] = {
            "kind": "rag_pipeline",
            "version": PIPELINE_DSL_VERSION,
            "rag_pipeline": {
                "name": self.name,
                "description": self.description,
                "icon": self.icon,
                "icon_type": "emoji",
                "icon_background": self.icon_background,
            },
            "workflow": workflow,
        }
        if self._dependencies:
            document["dependencies"] = self._dependencies
        return document

    def run(self, *_args: Any, **_kwargs: Any) -> Any:
        """Not available: a pipeline's ends are both the server.

        A datasource reads from Dify's storage and a knowledge-index node
        writes into a knowledge base, so there is nothing for graphon to run
        here even though everything between them would run.
        """
        msg = (
            "A pipeline cannot run locally: its datasource and knowledge-index "
            "nodes are Dify's own. Deploy it with "
            "DifyManagement.pipelines.deploy(pipeline) and run it there."
        )
        raise WorkflowError(msg)

    def __repr__(self) -> str:
        return f"Pipeline(name={self.name!r}, nodes={len(self._nodes)})"
