"""Whether reranking that is configured is also applied.

Storing a setting and *using* it are two questions, and only the second needs
a model. Both tests here ask it the only way it can be asked — run the same
query over the same chunks with and without the setting, and compare:

* a rerank model on a knowledge base, searched through the knowledge API;
* a weighted merge on a workflow's knowledge node over two bases, run on Dify.

They cost money — embeddings for sixteen short chunks, a few queries and one
rerank call — so they are marked billed and skip themselves unless
``DIFY_LIVE_TESTS`` is set:

    DIFY_LIVE_TESTS=1 uv run pytest tests/live/test_billed_rerank.py -m billed
"""

import time
import uuid

import pytest

from dify_client import DifyApp, retrieval_model
from dify_client.workflow import Workflow, text_input, weighted_score

from .conftest import HARNESS_DATASET, HARNESS_PREFIX, billed

#: One clearly relevant chunk, one near-miss, two unrelated.
CHUNKS = [
    "Refunds are issued within 5 business days to the original payment method.",
    "Our return policy allows exchanges within 30 days of purchase.",
    "The Tokyo office is open from 9am to 6pm on weekdays.",
    "Password resets are handled by the IT helpdesk, not support.",
]
QUERY = "how long does a refund take?"


def _model(management, model_type: str) -> str:
    names = management.models.names(model_type)
    if not names:
        pytest.skip(f"this Dify has no {model_type} provider configured")
    return names[0]


def _indexed(knowledge, name: str, retrieval: dict, embedding: str, chunks=CHUNKS):
    """A knowledge base holding ``chunks``, indexed and ready to search."""
    base = knowledge.datasets.create(
        f"{HARNESS_DATASET}-{name}-{uuid.uuid4().hex[:6]}",
        indexing_technique="high_quality",
        embedding=embedding,
        retrieval=retrieval,
    )
    documents = knowledge.documents(base)
    for chunk in chunks:
        documents.create(text=chunk, name=chunk[:24], indexing_technique="high_quality")
    for _ in range(60):
        statuses = [getattr(d, "indexing_status", "") for d in documents.list().items]
        if statuses and all(status == "completed" for status in statuses):
            return base
        time.sleep(2)
    pytest.fail(f"{name} never finished indexing: {statuses}")


@billed
def test_a_rerank_model_changes_what_retrieval_scores(knowledge, management):
    """The setting is only worth having if Dify calls the model, so check that.

    Comparing two bases rather than asserting a number keeps this about
    whether the model ran, not about how confident this week's model is.
    """
    embedding = _model(management, "text-embedding")
    rerank = _model(management, "rerank")

    plain = _indexed(
        knowledge,
        "plain",
        retrieval_model(search="hybrid_search", top_k=4),
        embedding,
    )
    try:
        reranked = _indexed(
            knowledge,
            "reranked",
            retrieval_model(search="hybrid_search", top_k=4, rerank=rerank),
            embedding,
        )
        try:
            without = knowledge.datasets.search(plain, QUERY)
            with_rerank = knowledge.datasets.search(reranked, QUERY)

            assert len(without) == len(with_rerank) == len(CHUNKS)
            # The refund chunk answers the question either way; what the rerank
            # model changes is how far the rest fall behind it.
            assert with_rerank[0].segment.content.startswith("Refunds are issued")
            assert [hit.score for hit in with_rerank] != [
                hit.score for hit in without
            ], "the rerank model was configured and never consulted"
        finally:
            knowledge.datasets.delete(reranked)
    finally:
        knowledge.datasets.delete(plain)


def _scores(management, service_api, name, datasets, **settings):
    """What a knowledge node over ``datasets`` scores the query, run on Dify.

    ``settings`` are passed to ``wf.knowledge``; ``raw=`` edits the node's
    retrieval config afterwards, which is how the document the SDK used to
    write is rebuilt without keeping the bug around to write it.
    """
    raw = settings.pop("raw", None)
    wf = Workflow(f"{HARNESS_PREFIX}-{name}-{uuid.uuid4().hex[:6]}")
    start = wf.start([text_input("q")])
    hits = wf.knowledge(start["q"], [d.id for d in datasets], top_k=4, **settings)
    if raw:
        for key, value in raw.items():
            setattr(hits.data.multiple_retrieval_config, key, value)
    wf.connect(start, hits, wf.end({"hits": hits.output}))

    result = management.apps.deploy(wf)
    try:
        result.raise_for_stage()
        with DifyApp(result.api_key, base_url=service_api, user=HARNESS_PREFIX) as app:
            run = app.workflows.runs.create({"q": QUERY})
        assert run.status == "succeeded", run.error
        return {
            hit["content"]: round(float(hit["metadata"]["score"]), 6)
            for hit in run.outputs["hits"]
        }
    finally:
        management.apps.delete(result.app_id)


@billed
def test_a_weighted_merge_changes_what_a_knowledge_node_scores(
    knowledge, management, service_api
):
    """``weights=`` on a knowledge node over several bases is applied only when
    ``reranking_enable`` is set, and the SDK used to leave it off — as Dify's
    own editor does.

    Three runs of one query over the same two bases: no reranking, the
    weighted merge as written now, and the same merge with the flag off as it
    was written before. Measured on Dify 1.17.1: the weighted run scored every
    chunk at exactly half the plain run, and the flag-off run scored exactly
    like the plain one.
    """
    embedding = _model(management, "text-embedding")
    semantic = retrieval_model(search="semantic_search", top_k=4)
    first = _indexed(knowledge, "merge-a", semantic, embedding, CHUNKS[0::2])
    try:
        second = _indexed(knowledge, "merge-b", semantic, embedding, CHUNKS[1::2])
        try:
            bases = [first, second]
            weights = weighted_score(embedding=embedding, vector=0.5, keyword=0.5)

            plain = _scores(management, service_api, "merge-plain", bases)
            weighted = _scores(
                management, service_api, "merge-weighted", bases, weights=weights
            )
            switched_off = _scores(
                management,
                service_api,
                "merge-off",
                bases,
                weights=weights,
                raw={"reranking_enable": False},
            )

            assert set(plain) == set(weighted) == set(CHUNKS)
            # The query shares no keyword with any chunk ("refund" is not
            # "Refunds"), so the keyword half scores nothing and a merge that
            # applied the weights scores every chunk at half its vector score.
            # Checking the ratio, rather than only that the scores moved, is
            # what shows it was *these* weights.
            for chunk in CHUNKS:
                assert weighted[chunk] == pytest.approx(plain[chunk] * 0.5, abs=1e-5), (
                    f"weights= was written and not applied: {plain} vs {weighted}"
                )
            assert switched_off == plain, (
                "with the flag off Dify should ignore the weights; if it does not, "
                "the fix is unnecessary and should be revisited"
            )
        finally:
            knowledge.datasets.delete(second)
    finally:
        knowledge.datasets.delete(first)
