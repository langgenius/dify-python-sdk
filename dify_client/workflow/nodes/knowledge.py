"""Knowledge nodes: retrieving from a knowledge base, and filling one.

Three node types, all Dify's own rather than graphon's, because all three are
the server: retrieval reads an index, indexing writes one, and a datasource is
where documents come from.

The supporting models (reranking, weighted score, metadata filtering) come
from Dify's ``core.rag.entities``, which is not published as a package, so
they are restated here. :mod:`dify_client.search` builds the same settings as
plain data for the Service API; these models are what validate that data on
its way into a node.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from graphon.entities.base_node_data import BaseNodeData
from graphon.enums import BuiltinNodeTypes
from graphon.nodes.llm.entities import ModelConfig
from pydantic import BaseModel, ConfigDict, Field

from ._errors import NodeError
from .agents import NodeInput

__all__ = [
    "DATASOURCE",
    "KNOWLEDGE_INDEX",
    "KNOWLEDGE_RETRIEVAL",
    "DatasourceNodeData",
    "KeywordSetting",
    "KnowledgeIndexNodeData",
    "KnowledgeRetrievalNodeData",
    "MetadataCondition",
    "MetadataFilteringCondition",
    "MultipleRetrievalConfig",
    "RerankingModelConfig",
    "SingleRetrievalConfig",
    "VectorSetting",
    "WeightedScoreConfig",
]

#: The DSL node types, as Dify spells them.
KNOWLEDGE_RETRIEVAL = BuiltinNodeTypes.KNOWLEDGE_RETRIEVAL
KNOWLEDGE_INDEX = "knowledge-index"
DATASOURCE = BuiltinNodeTypes.DATASOURCE

#: What Dify's editor defaults ``top_k`` to, from `web/config/index.ts`.
DEFAULT_TOP_K = 4

#: What a knowledge base built by a pipeline is searched with when the caller
#: does not say — the value this builder has always written there.
INDEX_TOP_K = 3

#: How chunks may be shaped, as Dify's index processor spells them.
CHUNK_STRUCTURES = ("text_model", "hierarchical_model", "qa_model")

#: Whether chunks are embedded or only keyword-indexed.
INDEXING_TECHNIQUES = ("economy", "high_quality")


class RerankingModelConfig(BaseModel):
    """The rerank model a multiple-dataset retrieval scores with.

    Dify accepts two spellings of the same thing and this is the one the
    workflow layer writes; its own model declares ``provider``/``model`` as
    validation aliases of these names.
    """

    model_config = ConfigDict(populate_by_name=True)

    reranking_provider_name: str = Field(validation_alias="provider")
    reranking_model_name: str = Field(validation_alias="model")


class VectorSetting(BaseModel):
    """The semantic half of a weighted score."""

    vector_weight: float
    embedding_provider_name: str
    embedding_model_name: str


class KeywordSetting(BaseModel):
    """The keyword half of a weighted score."""

    keyword_weight: float


class WeightedScoreConfig(BaseModel):
    """Scoring by weights rather than by a rerank model."""

    vector_setting: VectorSetting
    keyword_setting: KeywordSetting


class MultipleRetrievalConfig(BaseModel):
    """How several knowledge bases are searched and merged."""

    top_k: int
    score_threshold: float | None = None
    reranking_mode: str = "reranking_model"
    reranking_enable: bool = True
    reranking_model: RerankingModelConfig | None = None
    weights: WeightedScoreConfig | None = None


class SingleRetrievalConfig(BaseModel):
    """The model that picks *which* knowledge base to search."""

    model: ModelConfig


class MetadataCondition(BaseModel):
    """One metadata field, compared against a value."""

    name: str
    comparison_operator: str
    value: str | Sequence[str] | int | float | None = None


class MetadataFilteringCondition(BaseModel):
    """Which documents are eligible before retrieval runs."""

    logical_operator: Literal["and", "or"] | None = "and"
    conditions: list[MetadataCondition] | None = None


class KnowledgeRetrievalNodeData(BaseNodeData):
    """A knowledge retrieval node, as Dify's own entity declares it.

    ``dataset_ids`` are workspace knowledge base ids — ``DifyKnowledge.datasets``
    lists them. Dify's editor refuses a node with none, so the builder does too.
    """

    type: str = KNOWLEDGE_RETRIEVAL
    query_variable_selector: list[str] | None = None
    query_attachment_selector: list[str] | None = None
    dataset_ids: list[str] = Field(default_factory=list)
    retrieval_mode: Literal["single", "multiple"] = "multiple"
    multiple_retrieval_config: MultipleRetrievalConfig | None = None
    single_retrieval_config: SingleRetrievalConfig | None = None
    metadata_filtering_mode: Literal["disabled", "automatic", "manual"] | None = (
        "disabled"
    )
    metadata_model_config: ModelConfig | None = None
    metadata_filtering_conditions: MetadataFilteringCondition | None = None


class DatasourceNodeData(BaseNodeData):
    """Where documents come from: a file, an online drive, a website.

    A datasource starts a knowledge pipeline the way a start node starts a
    workflow, and Dify accepts one in an app workflow too — verified by
    importing and publishing one against 1.17.1. The provider identifiers come
    from an installed datasource plugin.
    """

    type: str = DATASOURCE
    plugin_id: str
    provider_name: str
    provider_type: str
    datasource_name: str | None = "local_file"
    datasource_configurations: dict[str, Any] | None = None
    plugin_unique_identifier: str | None = None
    datasource_parameters: dict[str, NodeInput] | None = None


class KnowledgeIndexNodeData(BaseNodeData):
    """The other end of a knowledge pipeline: chunks written into a knowledge base.

    ``chunk_structure`` says which shape the chunks arrive in — Dify's own
    general, parent-child and question-answer structures — and
    ``index_chunk_variable_selector`` points at the variable holding them.
    """

    type: str = KNOWLEDGE_INDEX
    chunk_structure: str
    index_chunk_variable_selector: list[str]
    indexing_technique: str | None = None
    summary_index_setting: dict[str, Any] | None = None
    # Dify's own model does not declare these two and its editor writes them,
    # so they are named here rather than left to ride along as extras.
    keyword_number: int | None = None
    retrieval_model: dict[str, Any] | None = None
    embedding_model_provider: str | None = None
    embedding_model: str | None = None


# -- building one, and the rules Dify applies to each ----------------------
#
# These carry what Dify accepts, so they live with the schemas rather than
# with the document that adds them: a rule about retrieval modes is a fact
# about this node, not about workflows.


def retrieval_data(
    *,
    query: Sequence[str],
    datasets: Sequence[str],
    settings: MultipleRetrievalConfig | None,
    model: tuple[str, str] | None,
    metadata: MetadataFilteringCondition | None,
    title: str,
) -> KnowledgeRetrievalNodeData:
    """A knowledge retrieval node, checked the way Dify's editor checks one.

    ``model`` is the provider and name that pick *which* base to search, which
    only single-mode retrieval has; ``settings`` is how a merge of several is
    scored, which only multiple-mode has. Exactly one of them applies.
    """
    if not datasets:
        msg = (
            "A knowledge node needs at least one knowledge base id. "
            "DifyKnowledge.datasets.list() shows what the workspace has."
        )
        raise NodeError(msg)
    single = (
        SingleRetrievalConfig(
            model=ModelConfig(provider=model[0], name=model[1], mode="chat")
        )
        if model is not None
        else None
    )
    return KnowledgeRetrievalNodeData(
        title=title,
        query_variable_selector=list(query),
        dataset_ids=list(datasets),
        retrieval_mode="single" if single is not None else "multiple",
        single_retrieval_config=single,
        multiple_retrieval_config=settings,
        metadata_filtering_mode="manual" if metadata is not None else "disabled",
        metadata_filtering_conditions=metadata,
    )


def check_retrieval_mode(
    *,
    mode: str,
    model: str | None,
    rerank: str | None,
    weights: Mapping[str, Any] | None,
    top_k: int | None = None,
    score_threshold: float | None = None,
) -> None:
    """Refuse the combinations Dify accepts and then cannot run.

    Kept apart from :func:`retrieval_data` because it is about the arguments a
    caller wrote, and names them back.
    """
    if mode not in ("multiple", "single"):
        msg = f"mode={mode!r} is not one Dify knows. Use 'multiple' or 'single'."
        raise NodeError(msg)
    if mode == "single" and model is None:
        msg = (
            "mode='single' has a model pick which knowledge base to search, "
            "so it needs model='provider/plugin/name:model'."
        )
        raise NodeError(msg)
    if mode == "multiple" and model is not None:
        msg = (
            "model= chooses between knowledge bases, which only mode='single' "
            "does. For mode='multiple', rerank= is what takes a model."
        )
        raise NodeError(msg)
    if mode == "single" and (rerank is not None or weights is not None):
        msg = (
            "rerank= and weights= score a merge of several knowledge bases, "
            "and mode='single' searches one. Use mode='multiple'."
        )
        raise NodeError(msg)
    if mode == "single" and (top_k is not None or score_threshold is not None):
        # A single-mode node carries no retrieval config at all, so these were
        # being dropped on the floor: the search ran with the knowledge base's
        # own settings and the numbers the caller wrote never reached Dify.
        given = ", ".join(
            name
            for name, value in (("top_k", top_k), ("score_threshold", score_threshold))
            if value is not None
        )
        msg = (
            f"{given} is a setting on the retrieval mode='single' does not "
            "have: Dify searches the base the model picked with that base's "
            "own settings. Use mode='multiple' to set it here, or set it on "
            "the knowledge base with knowledge.datasets.update()."
        )
        raise NodeError(msg)
    if rerank is not None and weights is not None:
        msg = (
            "rerank= scores with a model and weights= blends the scores "
            "arithmetically. Dify runs one or the other, so pass one."
        )
        raise NodeError(msg)


def index_data(
    *,
    chunks: Sequence[str],
    structure: str,
    indexing: str,
    embedding: tuple[str, str] | None,
    settings: Mapping[str, Any],
    keyword_number: int | None,
    summary: Mapping[str, Any] | None,
    title: str,
) -> KnowledgeIndexNodeData:
    """A knowledge-index node: the far end of a pipeline.

    ``structure`` and ``indexing`` are spelled the way Dify's index processor
    spells them, and a value it does not index is refused here rather than at
    import.
    """
    if structure not in CHUNK_STRUCTURES:
        accepted = ", ".join(repr(s) for s in CHUNK_STRUCTURES)
        msg = f"structure={structure!r} is not one Dify indexes. Use {accepted}."
        raise NodeError(msg)
    if indexing not in INDEXING_TECHNIQUES:
        accepted = ", ".join(repr(t) for t in INDEXING_TECHNIQUES)
        msg = f"indexing={indexing!r} is not one Dify knows. Use {accepted}."
        raise NodeError(msg)
    provider, model = embedding if embedding is not None else (None, None)
    return KnowledgeIndexNodeData(
        title=title,
        chunk_structure=structure,
        index_chunk_variable_selector=list(chunks),
        indexing_technique=indexing,
        # Dify reads this one only for an economy base; sending it with a
        # high-quality one is a setting that silently does nothing.
        keyword_number=keyword_number if indexing == "economy" else None,
        retrieval_model=dict(settings),
        embedding_model_provider=provider,
        embedding_model=model,
        summary_index_setting=dict(summary) if summary is not None else None,
    )


def datasource_data(
    *,
    plugin_id: str,
    provider: str,
    provider_type: str,
    name: str,
    config: Mapping[str, Any] | None,
    parameters: Mapping[str, NodeInput] | None,
    plugin_unique_identifier: str | None,
    title: str,
) -> DatasourceNodeData:
    """A datasource node: where a pipeline's documents come from."""
    return DatasourceNodeData(
        title=title,
        plugin_id=plugin_id,
        provider_name=provider,
        provider_type=provider_type,
        datasource_name=name,
        datasource_configurations=dict(config) if config is not None else None,
        plugin_unique_identifier=plugin_unique_identifier,
        datasource_parameters=dict(parameters) if parameters else None,
    )


def check_index_settings(
    *,
    indexing: str,
    embedding: str | None,
    retrieval: Mapping[str, Any] | None,
    search: str | None,
    rerank: str | None,
    weights: Mapping[str, Any] | None,
    top_k: int | None = None,
    score_threshold: float | None = None,
) -> None:
    """Refuse the argument combinations that cannot mean anything.

    ``retrieval`` is the whole search configuration; the other three are its
    parts. Passing both would leave one of them silently unused, which is the
    kind of setting that looks applied and is not.
    """
    if indexing == "high_quality" and embedding is None:
        # `retrieval` is how the base is searched; the embedding model is what
        # the chunks are stored as. One does not stand in for the other, and a
        # base written with neither has nothing to search.
        msg = (
            "indexing='high_quality' embeds every chunk, so it needs "
            "embedding='provider/plugin/name:model'. Use indexing='economy' "
            "for keyword search without a model."
        )
        raise NodeError(msg)
    if retrieval is not None:
        # top_k and score_threshold are parts too. They had a non-None default,
        # so passing one beside retrieval= could not be told from not passing
        # it, and the number was dropped.
        given = [
            name
            for name, value in (
                ("search", search),
                ("rerank", rerank),
                ("weights", weights),
                ("top_k", top_k),
                ("score_threshold", score_threshold),
            )
            if value is not None
        ]
        if given:
            listed = ", ".join(f"{name}=" for name in given)
            msg = (
                f"retrieval= is the whole search configuration, so {listed} "
                "has nothing left to set. Put it inside retrieval=, or drop "
                "retrieval= and pass the parts."
            )
            raise NodeError(msg)


def default_search(indexing: str) -> str:
    """How a base is searched when the caller did not say.

    An economy base is searched by keyword whatever this says — Dify overrides
    it — but the setting is stored and starts mattering if the base is
    upgraded to high quality later.
    """
    return "keyword_search" if indexing == "economy" else "semantic_search"
