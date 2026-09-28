"""How a knowledge base is searched, built once and used in three places.

The two states these keep apart were both bugs waiting to happen: a score
threshold that is stored and ignored, and two reranking modes that look like
one setting.
"""

from __future__ import annotations

import pytest

from dify_client import retrieval_model, weighted_score

RERANK = "langgenius/cohere/cohere:rerank-v3.5"
EMBEDDING = "langgenius/openai/openai:text-embedding-3-small"


def test_a_score_threshold_turns_on_the_flag_that_makes_it_count():
    """Dify stores the value and the flag separately and only reads it when set."""
    assert retrieval_model(score_threshold=0.4)["score_threshold_enabled"] is True
    assert retrieval_model(score_threshold=0.4)["score_threshold"] == 0.4


def test_no_threshold_is_not_a_threshold_of_zero():
    settings = retrieval_model()
    assert settings["score_threshold_enabled"] is False
    assert settings["score_threshold"] is None


def test_a_rerank_model_is_split_the_way_every_model_reference_is():
    settings = retrieval_model(rerank=RERANK)

    assert settings["reranking_enable"] is True
    assert settings["reranking_mode"] == "reranking_model"
    assert settings["reranking_model"] == {
        "reranking_provider_name": "langgenius/cohere/cohere",
        "reranking_model_name": "rerank-v3.5",
    }


def test_weighted_score_selects_itself_by_mode_and_calls_no_model():
    settings = retrieval_model(weights=weighted_score(embedding=EMBEDDING))

    assert settings["reranking_mode"] == "weighted_score"
    assert settings["reranking_enable"] is False
    assert settings["reranking_model"] is None


def test_the_two_reranking_modes_are_not_combined():
    with pytest.raises(ValueError, match="one or the other"):
        retrieval_model(rerank=RERANK, weights=weighted_score(embedding=EMBEDDING))


def test_a_search_method_dify_does_not_have_is_rejected():
    with pytest.raises(ValueError, match="not one Dify knows"):
        retrieval_model(search="vector_search")


def test_a_model_reference_without_a_model_name_is_rejected():
    with pytest.raises(ValueError, match="missing a model name"):
        retrieval_model(rerank="langgenius/cohere/cohere")


def test_weights_name_the_model_the_vectors_were_built_with():
    weights = weighted_score(embedding=EMBEDDING, vector=0.6, keyword=0.4)

    assert weights["vector_setting"] == {
        "vector_weight": 0.6,
        "embedding_provider_name": "langgenius/openai/openai",
        "embedding_model_name": "text-embedding-3-small",
    }
    assert weights["keyword_setting"] == {"keyword_weight": 0.4}


def test_a_number_is_written_the_way_dify_stores_one():
    """`when(count, ">", 5)` raised a pydantic error about a field nobody
    mentioned; Dify's editor stores numbers as text."""
    from dify_client.workflow import of_file, when
    from dify_client.workflow.refs import VarRef

    count = VarRef("start", "count")

    assert when(count, ">", 5).value == "5"
    assert when(count, ">", 2.5).value == "2.5"
    assert of_file("size", ">", 1024).value == "1024"
    # A boolean is a value Dify has, and None means "this compares nothing".
    assert when(count, "is", True).value is True
    assert when(count, "empty").value is None


class TestAWeightedMergeIsSwitchedOnOnTheNode:
    """Dify merges several knowledge bases with the weights only when
    ``reranking_enable`` is set, and ``weights=`` left it off, so a knowledge
    node over two bases merged with no weighting at all."""

    def node(self, **settings):
        from dify_client.workflow import Workflow, text_input, weighted_score

        wf = Workflow("w")
        start = wf.start([text_input("q")])
        return wf.knowledge(start["q"], ["a", "b"], **settings), weighted_score

    def test_weights_on_a_node_enable_the_merge(self):
        from dify_client.workflow import weighted_score

        node, _ = self.node(
            weights=weighted_score(
                embedding="langgenius/openai/openai:text-embedding-3-small"
            )
        )
        config = node.data.multiple_retrieval_config
        assert config.reranking_mode == "weighted_score"
        assert config.reranking_enable is True

    def test_a_node_with_neither_is_left_off(self):
        node, _ = self.node()
        assert node.data.multiple_retrieval_config.reranking_enable is False

    def test_a_knowledge_base_s_own_settings_are_unchanged(self):
        """There the flag is not the switch: hybrid search reads the weights."""
        from dify_client.workflow import retrieval_model, weighted_score

        settings = retrieval_model(
            search="hybrid_search",
            weights=weighted_score(
                embedding="langgenius/openai/openai:text-embedding-3-small"
            ),
        )
        assert settings["reranking_enable"] is False
