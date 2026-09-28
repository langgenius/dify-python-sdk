"""How a knowledge base is searched: the retrieval settings Dify stores on it.

The same block of settings appears in three places — creating a knowledge base
over the Service API, the knowledge-index node of a pipeline, and the
knowledge-retrieval node of a workflow — so it is built in one place rather
than spelled out three times.

Two things about it are easy to get wrong, and both were read out of Dify's
retrieval code rather than its documentation:

* **A score threshold has an enable flag next to it.** A threshold with the
  flag off is stored and ignored, which reads as a filter that does nothing.
  Passing ``score_threshold=`` here turns the flag on; leaving it out turns it
  off, and those are the two states.
* **An ``economy`` knowledge base is always searched by keyword**, whatever
  ``search`` says: it has no embeddings to compare against. The setting is
  kept because the base can be switched to ``high_quality`` later.

Reranking has two modes and they do different work. A **rerank model** scores
the merged results with a model — ``rerank="langgenius/cohere/cohere:rerank-v3.5"``
— and Dify only reaches for it when ``reranking_enable`` is true. **Weighted
score** instead blends the vector and keyword scores arithmetically, with no
model call, so it leaves ``reranking_enable`` off and is selected by the mode.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "SEARCH_METHODS",
    "retrieval_model",
    "split_model",
    "weighted_score",
]

#: How Dify may search a knowledge base.
SEARCH_METHODS = (
    "semantic_search",
    "full_text_search",
    "hybrid_search",
    "keyword_search",
)


def split_model(reference: str, what: str = "model") -> tuple[str, str]:
    """Split Dify's ``provider/plugin/vendor:model`` into provider and name.

    Raises ``ValueError``; callers that speak a different vocabulary wrap it.
    """
    provider, _, name = reference.rpartition(":")
    if not provider or not name:
        msg = (
            f"{what}={reference!r} is missing a model name. "
            "Use 'provider/plugin/name:model', "
            "e.g. 'langgenius/openai/openai:text-embedding-3-small'."
        )
        raise ValueError(msg)
    return provider, name


def weighted_score(
    *,
    embedding: str,
    vector: float = 0.7,
    keyword: float = 0.3,
) -> dict[str, Any]:
    """Blend vector and keyword scores instead of calling a rerank model.

    ``embedding`` is the model the vector half was indexed with — the blend is
    computed against those embeddings, so naming a different model here scores
    against vectors that do not exist::

        retrieval_model(search="hybrid_search",
                        weights=weighted_score(embedding=EMBEDDING_MODEL))
    """
    provider, name = split_model(embedding, "embedding")
    return {
        "vector_setting": {
            "vector_weight": vector,
            "embedding_provider_name": provider,
            "embedding_model_name": name,
        },
        "keyword_setting": {"keyword_weight": keyword},
    }


def retrieval_model(
    *,
    search: str = "semantic_search",
    top_k: int = 3,
    score_threshold: float | None = None,
    rerank: str | None = None,
    weights: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``retrieval_model`` block a knowledge base is searched by::

        base = knowledge.datasets.create(
            "handbook",
            indexing_technique="high_quality",
            embedding="langgenius/openai/openai:text-embedding-3-small",
            retrieval=retrieval_model(rerank="langgenius/cohere/cohere:rerank-v3.5"),
        )

    ``rerank`` and ``weights`` are the two reranking modes and only one applies
    at a time.
    """
    if search not in SEARCH_METHODS:
        accepted = ", ".join(SEARCH_METHODS)
        msg = f"search={search!r} is not one Dify knows. Use one of: {accepted}."
        raise ValueError(msg)
    if rerank is not None and weights is not None:
        msg = (
            "rerank= scores with a model and weights= blends the scores "
            "arithmetically. Dify runs one or the other, so pass one."
        )
        raise ValueError(msg)

    settings: dict[str, Any] = {
        "search_method": search,
        "top_k": top_k,
        # The flag is what makes the threshold count; the two are one setting
        # in Dify's UI and two fields on the wire.
        "score_threshold_enabled": score_threshold is not None,
        "score_threshold": score_threshold,
        "reranking_enable": False,
        "reranking_mode": "reranking_model",
        "reranking_model": None,
        "weights": None,
    }
    if rerank is not None:
        provider, name = split_model(rerank, "rerank")
        settings["reranking_enable"] = True
        settings["reranking_model"] = {
            "reranking_provider_name": provider,
            "reranking_model_name": name,
        }
    elif weights is not None:
        # Weighted score calls no model, and Dify reads the rerank model only
        # when reranking_enable is set — so the mode is what selects it.
        settings["reranking_mode"] = "weighted_score"
        settings["weights"] = dict(weights)
    return settings
