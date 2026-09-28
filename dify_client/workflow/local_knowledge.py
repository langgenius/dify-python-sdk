"""Running the knowledge node without a Dify: a stand-in for retrieval.

Dify serves ``knowledge-retrieval`` from its own ``core.workflow.nodes``
rather than from graphon, because retrieval *is* the server: a vector store,
an embedding model, a rerank model and the knowledge bases a workspace owns.
graphon knows the node type — it is in its enum — and ships no implementation,
so ``loads()`` refuses a document containing one with "Unsupported node
types".

This module closes that gap for a local run, the same way ``StubLLM`` and
``LocalSandbox`` stand in for a model and a sandbox. What answers the node is a
``Retriever``: ``StubKnowledge`` returns canned chunks and records what it was
asked, which is what lets the workflow *around* the retrieval — prompt
assembly, branching, output shaping — be tested offline::

    know = StubKnowledge(["Refunds take 5 business days."])
    result = wf.run({"q": "refund?"}, knowledge=know)
    assert know.calls[0].query == "refund?"

A stub is not a claim about what a real knowledge base returns. Retrieval
quality is a property of the server's index, so assert it against one —
``wf.run_live()``, or ``DifyKnowledge.datasets.search()`` for retrieval on its
own.

The node's data lives with the other Dify-owned node types, in
:mod:`dify_client.workflow.nodes.knowledge`.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from graphon.enums import WorkflowNodeExecutionStatus
from graphon.node_events.base import NodeRunResult
from graphon.nodes.base.node import Node
from graphon.runtime.init_params import InitParams
from graphon.runtime.runtime_state import RuntimeState

from .nodes.knowledge import (
    DEFAULT_TOP_K,
    KNOWLEDGE_RETRIEVAL,
    KnowledgeRetrievalNodeData,
)

__all__ = [
    "Chunk",
    "RetrievalCall",
    "Retriever",
    "StubKnowledge",
    "knowledge_retriever",
]


# -- what answers a retrieval ----------------------------------------------


@dataclass(frozen=True)
class Chunk:
    """One piece of retrieved text, and where it came from.

    ``score`` is ``None`` when nothing scored the chunk, which is not the same
    as a score of zero: an unscored set keeps the order it was given, a scored
    one is sorted by relevance the way Dify sorts it.
    """

    content: str
    title: str = ""
    score: float | None = None
    dataset_id: str = ""
    dataset_name: str = ""
    document_id: str = ""
    document_name: str = ""
    segment_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_source(self, position: int) -> dict[str, Any]:
        """Render as one item of the node's ``result`` array.

        The keys are Dify's ``Source``/``SourceMetadata``, including the
        leading underscore on ``_source`` that its serialization alias adds —
        downstream nodes index these names, so a different shape here would
        make a workflow that passes locally fail once deployed. The full set
        was read off a live 1.17 run rather than off the model, because
        ``files`` and ``summary`` are ``None`` and still present.
        """
        return {
            "metadata": {
                "_source": "knowledge",
                "dataset_id": self.dataset_id,
                "dataset_name": self.dataset_name,
                "document_id": self.document_id,
                "document_name": self.document_name or self.title,
                "data_source_type": "upload_file",
                "segment_id": self.segment_id,
                "retriever_from": "workflow",
                "score": self.score if self.score is not None else 0.0,
                "child_chunks": [],
                "segment_hit_count": 0,
                "segment_word_count": len(self.content),
                "segment_position": position,
                "segment_index_node_hash": None,
                "doc_metadata": None,
                "position": position,
                **dict(self.metadata),
            },
            "title": self.title or self.document_name,
            "files": None,
            "content": self.content,
            "summary": None,
        }


@dataclass(frozen=True)
class RetrievalCall:
    """One invocation of a retriever, as a test reads it back."""

    query: str
    dataset_ids: tuple[str, ...]
    top_k: int
    score_threshold: float | None


class Retriever(Protocol):
    """What a knowledge node calls to get its chunks.

    Implement this to retrieve from somewhere else — a live Dify, a local
    index, a fixture file. The node applies ``score_threshold`` and ``top_k``
    to whatever comes back, so an implementation may ignore both.
    """

    def retrieve(
        self,
        query: str,
        *,
        dataset_ids: Sequence[str],
        top_k: int,
        score_threshold: float | None,
    ) -> Sequence[Chunk]: ...


#: A stub's answer: fixed chunks, chunks per knowledge base, or a function of
#: the query.
Chunks = (
    Sequence[str | Chunk]
    | Mapping[str, Sequence[str | Chunk]]
    | Callable[[str, Sequence[str]], Sequence[str | Chunk]]
)


class StubKnowledge:
    """A stand-in for retrieval that returns canned chunks and records its calls.

    Three shapes of answer, in rising order of specificity::

        StubKnowledge(["a", "b"])                  # every query, every base
        StubKnowledge({"ds-1": ["a"], "ds-2": []}) # per knowledge base
        StubKnowledge(lambda q, ids: [q.upper()])  # decided by the query

    The mapping form is what proves a node was pointed at the base you meant:
    a base the node does not list never contributes.
    """

    def __init__(self, chunks: Chunks = ()):
        self._chunks = chunks
        #: Every retrieval this stub was asked for, in order.
        self.calls: list[RetrievalCall] = []

    def retrieve(
        self,
        query: str,
        *,
        dataset_ids: Sequence[str],
        top_k: int,
        score_threshold: float | None,
    ) -> Sequence[Chunk]:
        self.calls.append(
            RetrievalCall(
                query=query,
                dataset_ids=tuple(dataset_ids),
                top_k=top_k,
                score_threshold=score_threshold,
            )
        )
        return list(self._answer(query, dataset_ids))

    def _answer(self, query: str, dataset_ids: Sequence[str]) -> list[Chunk]:
        source = self._chunks
        if callable(source):
            return [_as_chunk(item, "") for item in source(query, list(dataset_ids))]
        if isinstance(source, Mapping):
            return [
                _as_chunk(item, dataset_id)
                for dataset_id in dataset_ids
                for item in source.get(dataset_id, ())
            ]
        first = dataset_ids[0] if dataset_ids else ""
        return [_as_chunk(item, first) for item in source]


def _as_chunk(item: str | Chunk, dataset_id: str) -> Chunk:
    if isinstance(item, Chunk):
        return item if item.dataset_id else _with_dataset(item, dataset_id)
    return Chunk(content=item, dataset_id=dataset_id)


def _with_dataset(chunk: Chunk, dataset_id: str) -> Chunk:
    return Chunk(
        content=chunk.content,
        title=chunk.title,
        score=chunk.score,
        dataset_id=dataset_id,
        dataset_name=chunk.dataset_name,
        document_id=chunk.document_id,
        document_name=chunk.document_name,
        segment_id=chunk.segment_id,
        metadata=chunk.metadata,
    )


# -- running one locally ---------------------------------------------------


class LocalKnowledgeRetrievalNode(Node[KnowledgeRetrievalNodeData]):
    """graphon's missing knowledge node, answered by a ``Retriever``.

    Defining this class registers it with graphon's node registry, which is
    what ``validate_node()`` consults; the builder in
    :func:`knowledge_retriever` is what constructs it. Registration is
    additive — graphon owns no implementation of this type to be shadowed.
    """

    node_type = KNOWLEDGE_RETRIEVAL

    def __init__(
        self,
        node_id: str,
        data: KnowledgeRetrievalNodeData,
        *,
        init_params: InitParams,
        runtime_state: RuntimeState,
        retriever: Retriever,
    ) -> None:
        super().__init__(
            node_id=node_id,
            data=data,
            init_params=init_params,
            runtime_state=runtime_state,
        )
        self._retriever = retriever

    @classmethod
    def version(cls) -> str:
        return "1"

    def _run(self) -> NodeRunResult:
        selector = self.node_data.query_variable_selector
        # Dify returns nothing rather than failing when no query is wired up.
        if not selector:
            return NodeRunResult(
                status=WorkflowNodeExecutionStatus.SUCCEEDED,
                inputs={},
                outputs={},
            )

        segment = self.runtime_state.variable_pool.get(selector)
        if segment is None:
            return NodeRunResult(
                status=WorkflowNodeExecutionStatus.FAILED,
                inputs={},
                error=(
                    f"Query variable {'.'.join(selector)} is not set. "
                    "Connect the node that produces it upstream of this one."
                ),
            )
        query = segment.value
        if not isinstance(query, str):
            return NodeRunResult(
                status=WorkflowNodeExecutionStatus.FAILED,
                inputs={},
                error="Query variable is not string type.",
            )

        config = self.node_data.multiple_retrieval_config
        top_k = config.top_k if config else DEFAULT_TOP_K
        threshold = config.score_threshold if config else None

        chunks = self._retriever.retrieve(
            query,
            dataset_ids=list(self.node_data.dataset_ids),
            top_k=top_k,
            score_threshold=threshold,
        )
        results = _rank(chunks, top_k=top_k, score_threshold=threshold)
        return NodeRunResult(
            status=WorkflowNodeExecutionStatus.SUCCEEDED,
            inputs={"query": query},
            outputs={
                "result": [
                    chunk.to_source(position) for position, chunk in enumerate(results)
                ]
            },
        )


def _rank(
    chunks: Sequence[Chunk],
    *,
    top_k: int,
    score_threshold: float | None,
) -> list[Chunk]:
    """Merge what the retriever returned the way Dify presents it.

    Sorting only happens when the chunks carry scores: an unscored stub is
    stating an order, and reordering it would make the fixture unreadable.
    """
    kept = list(chunks)
    if score_threshold is not None:
        kept = [c for c in kept if c.score is None or c.score >= score_threshold]
    if any(c.score is not None for c in kept):
        kept.sort(key=lambda c: c.score if c.score is not None else 0.0, reverse=True)
    return kept[:top_k]


@contextmanager
def knowledge_retriever(retriever: Retriever) -> Generator[None, None, None]:
    """Make every knowledge node in a workflow retrieve through ``retriever``.

    graphon decides which node types a document may contain while it plans the
    import, and which class builds each one while it builds the graph, so both
    have to be taught the type — patching only the builder leaves the document
    rejected before a builder is ever consulted.
    """
    import graphon.dsl.importer as importer
    from graphon.dsl.node_factory import SlimDslNodeFactory

    def build(
        factory: SlimDslNodeFactory,
        request: Any,
    ) -> LocalKnowledgeRetrievalNode:
        return LocalKnowledgeRetrievalNode(
            node_id=request.node_id,
            data=LocalKnowledgeRetrievalNode.validate_node_data(request.data),
            init_params=factory.init_params,
            runtime_state=factory.runtime_state,
            retriever=retriever,
        )

    supported = importer.SUPPORTED_DEFAULT_FACTORY_NODE_TYPES
    builders = SlimDslNodeFactory.NODE_BUILDERS
    importer.SUPPORTED_DEFAULT_FACTORY_NODE_TYPES = supported | {KNOWLEDGE_RETRIEVAL}
    SlimDslNodeFactory.NODE_BUILDERS = {  # type: ignore[misc]
        **builders,
        KNOWLEDGE_RETRIEVAL: build,
    }
    try:
        yield
    finally:
        importer.SUPPORTED_DEFAULT_FACTORY_NODE_TYPES = supported
        SlimDslNodeFactory.NODE_BUILDERS = builders  # type: ignore[misc]
