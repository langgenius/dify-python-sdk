"""Knowledge pipelines: a different document, with different rules.

The shapes here were deployed to a running Dify 1.17.1 before they were
written down. What the tests hold in place is the handful of ways a pipeline
is *not* an app — the version, the envelope, the ends of the graph, and the
three-part references its inputs use.
"""

from __future__ import annotations

import pytest
import yaml

from dify_client.workflow import Pipeline, VarRef, WorkflowError


def build() -> Pipeline:
    pipe = Pipeline("support-docs", description="Index the handbook.")
    files = pipe.datasource(
        plugin_id="langgenius/file", provider="file", name="upload-file"
    )
    index = pipe.knowledge_index(files["file"])
    pipe.connect(files, index)
    return pipe


def document(pipe: Pipeline) -> dict:
    return yaml.safe_load(pipe.to_yaml())


def test_a_pipeline_is_its_own_kind_of_document():
    doc = document(build())

    assert doc["kind"] == "rag_pipeline"
    assert doc["rag_pipeline"]["name"] == "support-docs"
    assert "app" not in doc


def test_a_pipeline_carries_the_dsl_version_its_importer_is_on():
    """0.1.0, not the app's 0.7.0: sending the app's makes Dify hold the import."""
    assert document(build())["version"] == "0.1.0"


def test_a_pipeline_declares_its_own_variables_section():
    doc = document(build())

    assert doc["workflow"]["rag_pipeline_variables"] == []


def test_a_pipeline_input_is_referenced_through_rag_and_its_node():
    """Three parts, not two: the input belongs to the datasource that asks for it."""
    pipe = build()
    source = pipe.nodes[0]
    url = pipe.variable(source, "source_url", label="URL")

    assert url.selector == ["rag", source.id, "source_url"]
    assert str(url) == f"{{{{#rag.{source.id}.source_url#}}}}"

    (declared,) = document(pipe)["workflow"]["rag_pipeline_variables"]
    assert declared["belong_to_node_id"] == source.id
    assert declared["variable"] == "source_url"
    assert declared["label"] == "URL"


def test_an_input_widget_dify_does_not_draw_is_rejected():
    pipe = build()
    with pytest.raises(WorkflowError, match="not an input Dify draws"):
        pipe.variable(pipe.nodes[0], "x", type="slider")


def test_the_same_input_is_not_declared_twice_on_one_node():
    pipe = build()
    pipe.variable(pipe.nodes[0], "url")
    with pytest.raises(WorkflowError, match="already declared"):
        pipe.variable(pipe.nodes[0], "url")


def test_a_pipeline_needs_a_datasource_to_start_at():
    pipe = Pipeline("no-source")
    chunks = pipe.code("def main(): return {'c': []}", outputs={"c": "array[string]"})
    index = pipe.knowledge_index(chunks["c"])
    pipe.connect(chunks, index)

    with pytest.raises(WorkflowError, match="starts at a datasource"):
        pipe.to_yaml()


def test_a_pipeline_needs_a_knowledge_base_to_end_at():
    pipe = Pipeline("no-base")
    files = pipe.datasource(plugin_id="langgenius/file", provider="file")
    text = pipe.extract_text(files["file"])
    pipe.connect(files, text)

    with pytest.raises(WorkflowError, match="ends at a knowledge base"):
        pipe.to_yaml()


def test_a_pipeline_does_not_offer_what_only_an_app_has():
    """An app is started by a caller and answers one; a pipeline does neither.

    These used to be inherited and callable, and a pipeline carrying an answer
    node passed validate(). They are on the app document now, so reaching for
    one is a mistake that never gets as far as Dify.
    """
    pipe = build()

    for name in ("start", "answer", "end", "webhook", "schedule", "conversation_var"):
        assert not hasattr(pipe, name), f"{name} is an app's, not a pipeline's"
    assert not hasattr(pipe, "run_live")


def test_a_start_node_added_by_hand_is_still_refused():
    """`add()` takes any node data, so validate() is the backstop."""
    from graphon.nodes.start.entities import StartNodeData

    pipe = build()
    pipe.add(StartNodeData(title="Start"))

    with pytest.raises(WorkflowError, match="does not belong in one"):
        pipe.to_yaml()


def test_a_knowledge_base_is_always_given_search_settings():
    """Dify refuses the import without them, naming a model the SDK cannot see."""
    index = document(build())["workflow"]["graph"]["nodes"][1]["data"]

    assert index["retrieval_model"]["search_method"] == "keyword_search"
    assert index["indexing_technique"] == "economy"


def test_high_quality_indexing_needs_the_model_that_embeds():
    pipe = Pipeline("embedded")
    files = pipe.datasource(plugin_id="langgenius/file", provider="file")
    with pytest.raises(WorkflowError, match="needs embedding="):
        pipe.knowledge_index(files["file"], indexing="high_quality")


def test_high_quality_indexing_records_the_embedding_model():
    pipe = Pipeline("embedded")
    files = pipe.datasource(plugin_id="langgenius/file", provider="file")
    index = pipe.knowledge_index(
        files["file"],
        indexing="high_quality",
        embedding="langgenius/openai/openai:text-embedding-3-small",
    )
    pipe.connect(files, index)

    data = document(pipe)["workflow"]["graph"]["nodes"][1]["data"]
    assert data["embedding_model_provider"] == "langgenius/openai/openai"
    assert data["embedding_model"] == "text-embedding-3-small"
    assert data["retrieval_model"]["search_method"] == "semantic_search"


def test_a_pipeline_cannot_be_run_locally():
    """Both of its ends are the server, so there is nothing to run here."""
    with pytest.raises(WorkflowError, match="cannot run locally"):
        build().run({})


def test_the_graph_between_the_ends_is_ordinary_workflow_work():
    """A pipeline is a workflow: the node helpers, edges and ids are the same."""
    pipe = Pipeline("with-a-middle")
    files = pipe.datasource(plugin_id="langgenius/file", provider="file")
    text = pipe.extract_text(files["file"], id="extract")
    index = pipe.knowledge_index(text.output, id="base")
    pipe.connect(files, text, index)

    doc = document(pipe)
    assert [n["data"]["type"] for n in doc["workflow"]["graph"]["nodes"]] == [
        "datasource",
        "document-extractor",
        "knowledge-index",
    ]
    assert len(doc["workflow"]["graph"]["edges"]) == 2


def test_a_knowledge_base_can_be_searched_by_a_rerank_model():
    pipe = Pipeline("reranked")
    files = pipe.datasource(plugin_id="langgenius/file", provider="file")
    index = pipe.knowledge_index(
        files["file"],
        indexing="high_quality",
        embedding="langgenius/openai/openai:text-embedding-3-small",
        search="hybrid_search",
        rerank="langgenius/cohere/cohere:rerank-v3.5",
        top_k=5,
    )
    pipe.connect(files, index)

    settings = document(pipe)["workflow"]["graph"]["nodes"][1]["data"][
        "retrieval_model"
    ]
    assert settings["search_method"] == "hybrid_search"
    assert settings["reranking_enable"] is True
    assert settings["reranking_model"] == {
        "reranking_provider_name": "langgenius/cohere/cohere",
        "reranking_model_name": "rerank-v3.5",
    }
    assert settings["top_k"] == 5


def test_a_knowledge_base_can_blend_scores_instead_of_calling_a_model():
    from dify_client import weighted_score

    pipe = Pipeline("weighted")
    files = pipe.datasource(plugin_id="langgenius/file", provider="file")
    index = pipe.knowledge_index(
        files["file"],
        indexing="high_quality",
        embedding="langgenius/openai/openai:text-embedding-3-small",
        search="hybrid_search",
        weights=weighted_score(
            embedding="langgenius/openai/openai:text-embedding-3-small",
            vector=0.8,
            keyword=0.2,
        ),
    )
    pipe.connect(files, index)

    settings = document(pipe)["workflow"]["graph"]["nodes"][1]["data"][
        "retrieval_model"
    ]
    assert settings["reranking_mode"] == "weighted_score"
    # Weighted score calls no model, and Dify only reads the rerank model when
    # this flag is set — so the mode is what selects it.
    assert settings["reranking_enable"] is False
    assert settings["weights"]["vector_setting"]["vector_weight"] == 0.8


def test_a_base_is_not_reranked_two_ways_at_once():
    from dify_client import weighted_score

    pipe = Pipeline("both")
    files = pipe.datasource(plugin_id="langgenius/file", provider="file")
    with pytest.raises(WorkflowError, match="one or the other"):
        pipe.knowledge_index(
            files["file"],
            rerank="langgenius/cohere/cohere:rerank-v3.5",
            weights=weighted_score(
                embedding="langgenius/openai/openai:text-embedding-3-small"
            ),
        )


def test_the_whole_search_block_and_its_parts_are_not_both_given():
    pipe = Pipeline("both-ways")
    files = pipe.datasource(plugin_id="langgenius/file", provider="file")
    with pytest.raises(WorkflowError, match="nothing left to set"):
        pipe.knowledge_index(
            files["file"],
            retrieval={"search_method": "semantic_search", "top_k": 3},
            rerank="langgenius/cohere/cohere:rerank-v3.5",
        )


def test_an_economy_base_is_searched_by_keyword_whatever_else_is_asked_for():
    """Dify forces keyword search on an economy base; the setting is still kept."""
    pipe = Pipeline("economy")
    files = pipe.datasource(plugin_id="langgenius/file", provider="file")
    index = pipe.knowledge_index(files["file"])
    pipe.connect(files, index)

    settings = document(pipe)["workflow"]["graph"]["nodes"][1]["data"][
        "retrieval_model"
    ]
    assert settings["search_method"] == "keyword_search"


def test_the_shared_graph_does_not_know_which_document_it_is_in():
    """The base must not import its subclasses, or the split is a circle.

    `graph.py` holds what an app and a pipeline share. If it reaches back into
    either, the two documents are one again and nothing stops an app-only node
    from being offered to a pipeline.
    """
    import ast
    import inspect

    from dify_client.workflow import graph, parts

    for module in (graph, parts):
        imported = {
            node.module
            for node in ast.walk(ast.parse(inspect.getsource(module)))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not {name for name in imported if name.endswith(("builder", "pipeline"))}
    # And the pieces know nothing of the graph they are assembled into.
    assert "graph" not in {
        node.module
        for node in ast.walk(ast.parse(inspect.getsource(parts)))
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_a_pipeline_cannot_ship_an_agent_dify_would_drop():
    """Verified against 1.17.1: the pipeline importer keeps neither binding.

    It accepts the document, publishes it, and drops `agent_packages` — so an
    inline agent deploys as a node bound to a package that is not there, and
    nothing says so until the run. The helpers live on the app document for
    that reason; `agent()`, whose strategy needs no binding, stays.
    """
    pipe = build()

    assert not hasattr(pipe, "inline_agent")
    assert not hasattr(pipe, "dify_agent")
    assert hasattr(pipe, "agent")


def test_an_input_can_belong_to_every_datasource_at_once():
    """Dify calls that scope `shared`, and keeps chunking settings there."""
    pipe = build()
    size = pipe.variable(None, "chunk_size", type="number", default=500)

    assert size.selector == ["rag", "shared", "chunk_size"]
    (declared,) = [v for v in document(pipe)["workflow"]["rag_pipeline_variables"]]
    assert declared["belong_to_node_id"] == "shared"
    assert declared["default_value"] == 500


def test_a_node_type_can_be_checked_without_a_document():
    """The point of moving the rules: they are about the node, not the graph.

    Each of these used to be reachable only by building a workflow and calling
    a helper on it; now the node module answers on its own.
    """
    import pytest

    from dify_client.workflow.nodes import knowledge, logic
    from dify_client.workflow.nodes._errors import NodeError

    with pytest.raises(NodeError, match="at least one knowledge base"):
        knowledge.retrieval_data(
            query=["start", "q"],
            datasets=[],
            settings=None,
            model=None,
            metadata=None,
            title="Knowledge",
        )
    with pytest.raises(NodeError, match="not an assignment Dify knows"):
        logic.assign_data(
            assignments=[(VarRef("conversation", "x"), "overwrite", "y")],
            title="Assign",
        )
    with pytest.raises(NodeError, match="not one Dify knows"):
        logic.iteration_data(
            node_id="it",
            items=["a", "b"],
            parallel=False,
            parallel_nums=1,
            on_error="explode",
            flatten=True,
            title="Iteration",
        )


def test_a_documents_helper_still_speaks_its_own_vocabulary():
    """A node's refusal reaches the caller as the error they already catch."""
    pipe = build()
    with pytest.raises(WorkflowError, match="not one Dify indexes"):
        pipe.knowledge_index(pipe.nodes[0]["file"], structure="nope")


def test_high_quality_indexing_needs_a_model_even_with_search_settings():
    """`retrieval` says how the base is searched, not what the chunks are.

    Passing one used to satisfy the check, and the DSL then carried a
    high-quality base with no embedding model at all.
    """
    from dify_client import retrieval_model

    pipe = Pipeline("embedded")
    files = pipe.datasource(plugin_id="langgenius/file", provider="file")
    with pytest.raises(WorkflowError, match="needs embedding="):
        pipe.knowledge_index(
            files["file"],
            indexing="high_quality",
            retrieval=retrieval_model(search="semantic_search"),
        )


def test_a_container_inside_a_container_belongs_to_it():
    """A nested one used to be written at the top level, leaving the outer
    container reported as empty."""
    from dify_client.workflow import Workflow, text_input

    wf = Workflow("nested")
    start = wf.start([text_input("q")])
    listing = wf.code(
        "def main(q): return {'xs': [q]}",
        variables={"q": start["q"]},
        outputs={"xs": "array[string]"},
    )
    with wf.iteration(listing["xs"], id="outer") as outer:
        with wf.loop(count=2, id="inner"):
            step = wf.template("x", id="body")
        outer.returns(step.output)
    wf.end({"o": outer.output})

    nodes = {n["id"]: n for n in document(wf)["workflow"]["graph"]["nodes"]}
    assert nodes["inner"]["parentId"] == "outer"
    assert nodes["body"]["parentId"] == "inner"


def test_a_start_marker_never_lands_on_a_name_already_taken():
    """The marker is a node too, and two nodes under one id is a broken graph."""
    from dify_client.workflow import Workflow, text_input

    wf = Workflow("clash")
    start = wf.start([text_input("q")])
    wf.template("mine", id="iterationstart")
    with wf.iteration(start["q"], id="iteration") as each:
        inner = wf.template("y")
        each.returns(inner.output)
    wf.end({"o": each.output})

    ids = [n.id for n in wf.nodes]
    assert len(ids) == len(set(ids))
    marker = next(n for n in wf.nodes if n.data.type == "iteration-start")
    # Whatever it ended up called, the container points at it.
    assert each.data.start_node_id == marker.id


def test_an_input_belongs_to_a_datasource_or_to_all_of_them():
    """Dify fills in the inputs of the datasource being used and nothing else,
    so one owned by a processing node reads as unset at run time."""
    pipe = build()
    text = pipe.extract_text(pipe.nodes[0]["file"], id="extract")
    pipe.variable(text, "nope")

    with pytest.raises(WorkflowError, match="is not a datasource"):
        pipe.to_yaml()


class TestTheWholeSearchBlockLeavesNoPartToSet:
    """``check_index_settings`` refused ``search=``, ``rerank=`` and
    ``weights=`` beside ``retrieval=`` and let ``top_k`` through: its default
    was 3, so passing one could not be told from not passing it, and the
    number was dropped."""

    def pipe(self):
        from dify_client.workflow import Pipeline

        pipe = Pipeline("p")
        files = pipe.datasource(
            plugin_id="langgenius/file", provider="file", name="upload-file", id="files"
        )
        return pipe, files

    @pytest.mark.parametrize("given", [{"top_k": 10}, {"score_threshold": 0.4}])
    def test_a_part_beside_the_whole_block_is_refused(self, given):
        from dify_client.workflow import WorkflowError, retrieval_model

        pipe, files = self.pipe()
        with pytest.raises(WorkflowError, match="retrieval= is the whole search"):
            pipe.knowledge_index(files["file"], retrieval=retrieval_model(), **given)

    def test_the_default_is_still_three(self):
        pipe, files = self.pipe()
        node = pipe.knowledge_index(files["file"])

        assert node.data.retrieval_model["top_k"] == 3
