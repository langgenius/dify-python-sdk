"""The shapes that ship with the SDK, and what each one promises.

A recipe is only worth having if it is known to work, so these assert the
parts a caller would otherwise have to discover: that the graph is wired, that
the grounding prompt actually says what to do when the context is thin, and
that each fragment hands back something the rest of the workflow can use.
The live half deploys them.
"""

from __future__ import annotations

from dify_client.workflow import StubKnowledge, StubLLM, Workflow, text_input
from dify_client.workflow.recipes import (
    GROUNDING,
    approval,
    extract_fields,
    grounded_answer,
    rag_answer,
)

MODEL = "langgenius/openai/openai:gpt-4o-mini"
DATASET = "00000000-0000-0000-0000-000000000000"


class TestRagAnswer:
    def test_it_is_a_chatflow_that_asks_and_answers(self):
        wf = rag_answer(dataset=DATASET, model=MODEL)

        assert wf.mode == "advanced-chat"
        assert [n.type for n in wf.nodes] == [
            "start",
            "knowledge-retrieval",
            "llm",
            "answer",
        ]

    def test_it_needs_no_wiring_of_its_own(self):
        """Every edge in it comes from a reference, which is the point."""
        wf = rag_answer(dataset=DATASET, model=MODEL)

        assert wf._edges == []
        assert {(e.source, e.target) for e in wf.edges} >= {
            ("start", "knowledge_retrieval"),
            ("knowledge_retrieval", "llm"),
            ("llm", "answer"),
        }

    def test_the_answer_is_the_model_reading_what_was_retrieved(self):
        wf = rag_answer(dataset=DATASET, model=MODEL)
        stub = StubLLM("Five business days.")

        result = wf.run(
            {"question": "how long do refunds take?"},
            knowledge=StubKnowledge(["Refunds take 5 business days."]),
            llm=stub,
        )

        assert result["answer"] == "Five business days."
        prompt = str(stub.calls[0][0].content)
        assert "Refunds take 5 business days." in prompt
        assert "how long do refunds take?" in prompt

    def test_the_grounding_prompt_says_what_to_do_when_it_does_not_know(self):
        """A model told only to "use the context" answers from memory instead."""
        assert "say you do not know" in GROUNDING
        assert "only" in GROUNDING

    def test_the_knowledge_base_and_its_rerank_reach_the_node(self):
        wf = rag_answer(
            dataset=[DATASET, "other"],
            model=MODEL,
            top_k=7,
            rerank="langgenius/cohere/cohere:rerank-v3.5",
        )
        hits = next(n for n in wf.nodes if n.type == "knowledge-retrieval")

        assert hits.data.dataset_ids == [DATASET, "other"]
        assert hits.data.multiple_retrieval_config.top_k == 7
        assert hits.data.multiple_retrieval_config.reranking_enable is True


class TestFragmentsCompose:
    def test_a_fragment_hands_back_a_node_the_rest_can_read(self):
        wf = Workflow("mine")
        start = wf.start([text_input("question")])
        reply = grounded_answer(wf, start["question"], dataset=DATASET, model=MODEL)
        wf.end({"answer": reply.output})

        result = wf.run(
            {"question": "q"},
            knowledge=StubKnowledge(["a fact"]),
            llm=StubLLM("an answer"),
        )
        assert result["answer"] == "an answer"

    def test_two_of_them_can_live_in_one_workflow(self):
        """Which is what `prefix` is for: the ids would collide otherwise."""
        wf = Workflow("two")
        start = wf.start([text_input("question")])
        first = grounded_answer(
            wf, start["question"], dataset=DATASET, model=MODEL, prefix="a_"
        )
        second = grounded_answer(
            wf, start["question"], dataset="other", model=MODEL, prefix="b_"
        )
        wf.end({"one": first.output, "two": second.output})

        assert {n.id for n in wf.nodes} >= {"a_hits", "a_answer", "b_hits", "b_answer"}

    def test_extracted_fields_are_named_outputs(self):
        wf = Workflow("extract")
        start = wf.start([text_input("email")])
        fields = extract_fields(
            wf,
            start["email"],
            {"order_id": "the order number", "urgent": "does it read as urgent"},
            model=MODEL,
            required=["order_id"],
        )
        wf.end({"order": fields["order_id"]})

        declared = {p.name: p for p in fields.data.parameters}
        assert declared["order_id"].required is True
        assert declared["urgent"].required is False
        assert declared["order_id"].description == "the order number"

    def test_an_approval_gate_has_the_arm_nobody_remembers(self):
        """Dify adds a timeout arm, and a workflow that ignores it stops there."""
        wf = Workflow("gate")
        start = wf.start([text_input("draft")])
        gate = approval(wf, "Ship this?")
        ship = wf.template("shipped", id="ship")
        revise = wf.template("revised", id="revise")
        wf.connect(start, gate)
        wf.connect(gate.case("approve"), ship)
        wf.connect(gate.case("reject"), revise)
        wf.connect(gate.timeout, revise)
        wf.end({"out": wf.merge(ship, revise).output})

        assert gate.handles == ("approve", "reject", "__timeout")


def test_a_node_handed_to_a_recipe_is_read_as_its_output():
    """It used to render as the node's id, so the prompt asked "Question: q".

    The knowledge half resolved it and the prompt half did not, which is the
    kind of split that only shows up in what the model was actually sent.
    """
    wf = Workflow("node-in")
    start = wf.start([text_input("raw")])
    cleaned = wf.template("{{ r }}", variables={"r": start["raw"]}, id="cleaned")
    reply = grounded_answer(wf, cleaned, dataset=DATASET, model=MODEL)

    prompt = reply.data.prompt_template[0].text
    assert "{{#cleaned.output#}}" in prompt
    assert "Question: cleaned" not in prompt


class TestFilePipeline:
    def test_it_is_the_chain_that_actually_indexes(self):
        """datasource → extractor → chunker → base. Without the chunker, Dify
        queues the document and fails indexing after the run looks fine."""
        from dify_client.workflow.recipes import CHUNKER, EXTRACTOR, file_pipeline

        pipe = file_pipeline(name="handbook")

        assert [n.type for n in pipe.nodes] == [
            "datasource",
            "tool",
            "tool",
            "knowledge-index",
        ]
        providers = [n.data.provider_id for n in pipe.nodes if n.type == "tool"]
        assert providers == [EXTRACTOR, CHUNKER]

    def test_the_index_reads_the_chunker_rather_than_the_extractor(self):
        from dify_client.workflow.recipes import file_pipeline

        pipe = file_pipeline(name="handbook")
        chunker = [n for n in pipe.nodes if n.type == "tool"][-1]
        index = next(n for n in pipe.nodes if n.type == "knowledge-index")

        assert index.data.index_chunk_variable_selector == [chunker.id, "result"]

    def test_the_chunker_reads_the_extractor_s_text(self):
        """A tool node's result is `text`, and the extractor declares no
        `output` (its schema adds `documents` and `images`). Dify's own
        templates read `output` from a variable aggregator placed between the
        two; reading it from the extractor named nothing, and a run on Dify
        indexed one chunk reading "tool.output" and reported success."""
        from dify_client.workflow.recipes import file_pipeline

        pipe = file_pipeline(name="handbook")
        extractor, chunker = [n for n in pipe.nodes if n.type == "tool"]

        assert chunker.data.tool_parameters["input_variable"].value == str(
            extractor["text"]
        )

    def test_its_search_settings_are_the_ones_asked_for(self):
        from dify_client.workflow.recipes import file_pipeline

        pipe = file_pipeline(
            name="handbook",
            indexing="high_quality",
            embedding="langgenius/openai/openai:text-embedding-3-small",
            rerank="langgenius/cohere/cohere:rerank-v3.5",
            search="hybrid_search",
        )
        index = next(n for n in pipe.nodes if n.type == "knowledge-index")

        assert index.data.embedding_model == "text-embedding-3-small"
        assert index.data.retrieval_model["reranking_enable"] is True
        assert index.data.retrieval_model["search_method"] == "hybrid_search"
