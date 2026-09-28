"""Tests for running workflows with a stubbed model."""

import pytest

from dify_client.workflow import (
    StubLLM,
    Workflow,
    WorkflowRunError,
    paragraph,
    run_with_stub,
    select,
    text_input,
)
from dify_client.workflow.testing import stub_models


def summarizer() -> Workflow:
    wf = Workflow("summarizer")
    start = wf.start([paragraph("draft"), select("tone", ["short", "long"])])
    prompt = wf.template(
        "Summarise, {{ tone }}:\n{{ draft }}",
        variables={"draft": start["draft"], "tone": start["tone"]},
        id="prompt",
    )
    llm = wf.llm(prompt.output, model="langgenius/openai/openai:gpt-4o-mini", id="llm")
    answer = wf.answer(llm.output)
    wf.connect(start, prompt, llm, answer)
    return wf


INPUTS = {"draft": "a very long document", "tone": "short"}


class TestWithoutAStub:
    def test_an_llm_node_cannot_be_built_without_credentials(self):
        """The failure arrives while building, so it is not a run result."""
        with pytest.raises(WorkflowRunError, match="Could not build the workflow"):
            summarizer().run(INPUTS)

    def test_the_error_points_at_the_way_out(self):
        with pytest.raises(WorkflowRunError, match="StubLLM"):
            summarizer().run(INPUTS)


class TestWithAStub:
    def test_a_stub_makes_the_workflow_runnable(self):
        result = summarizer().run(INPUTS, llm=StubLLM("a summary"))
        assert result.succeeded
        assert result["answer"] == "a summary"

    def test_the_stub_records_the_prompt_the_workflow_built(self):
        stub = StubLLM("a summary")
        summarizer().run(INPUTS, llm=stub)
        sent = str(stub.calls[0][0].content)
        assert sent == "Summarise, short:\na very long document"

    def test_the_reply_can_depend_on_the_prompt(self):
        stub = StubLLM(
            lambda msgs: "SHORT" if "short" in str(msgs[0].content) else "LONG"
        )
        assert summarizer().run(INPUTS, llm=stub)["answer"] == "SHORT"
        long_inputs = {**INPUTS, "tone": "long"}
        assert summarizer().run(long_inputs, llm=stub)["answer"] == "LONG"

    def test_intermediate_nodes_are_still_inspectable(self):
        result = summarizer().run(INPUTS, llm=StubLLM("a summary"))
        assert result.node("prompt")["output"].startswith("Summarise, short:")
        assert result.node("llm").succeeded

    def test_run_with_stub_works_on_a_raw_dsl_document(self):
        dsl = summarizer().to_yaml()
        result = run_with_stub(dsl, llm=StubLLM("hi"), inputs=INPUTS)
        assert result["answer"] == "hi"

    def test_every_llm_node_uses_the_stub(self):
        wf = Workflow("two-models")
        start = wf.start([text_input("q")])
        first = wf.llm(
            start["q"], model="langgenius/openai/openai:gpt-4o-mini", id="first"
        )
        second = wf.llm(
            first.output, model="langgenius/openai/openai:gpt-4o", id="second"
        )
        answer = wf.answer(second.output)
        wf.connect(start, first, second, answer)

        stub = StubLLM("ok")
        assert wf.run({"q": "hello"}, llm=stub).succeeded
        assert len(stub.calls) == 2


class TestStubIsolation:
    def test_the_patch_is_undone_afterwards(self):
        from graphon.dsl.node_factory import SlimDslNodeFactory

        before = SlimDslNodeFactory._create_slim_llm_runtime
        with stub_models(StubLLM("x")):
            assert SlimDslNodeFactory._create_slim_llm_runtime is not before
        assert SlimDslNodeFactory._create_slim_llm_runtime is before

    def test_the_patch_is_undone_after_a_failure(self):
        from graphon.dsl.node_factory import SlimDslNodeFactory

        before = SlimDslNodeFactory._resolve_slim_llm_settings
        with pytest.raises(RuntimeError):  # noqa: PT012 - the raise is the point
            with stub_models(StubLLM("x")):
                raise RuntimeError("boom")
        assert SlimDslNodeFactory._resolve_slim_llm_settings is before

    def test_credentials_are_required_again_after_stubbing(self):
        summarizer().run(INPUTS, llm=StubLLM("x"))
        with pytest.raises(WorkflowRunError):
            summarizer().run(INPUTS)


class TestStubBehaviour:
    def test_structured_output_is_explicitly_unsupported(self):
        stub = StubLLM("x")
        with pytest.raises(NotImplementedError, match="structured output"):
            stub.invoke_llm_with_structured_output(
                prompt_messages=[],
                json_schema={},
                model_parameters={},
                stop=None,
                stream=False,
            )

    def test_it_reports_a_model_schema(self):
        schema = StubLLM("x", model_name="fake-1").get_model_schema()
        assert schema.model == "fake-1"
