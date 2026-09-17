"""Testing a workflow must never require a real provider key.

The point of StubLLM being the default route is that a CI job running these
tests holds no credential worth stealing. These tests hold that line.
"""

import pytest

from dify_client.workflow import StubLLM, Workflow, WorkflowRunError, text_input

SECRET = "sk-LEAKME-1234567890"
CREDENTIALS = {
    "model_credentials": [{"vendor": "openai", "values": {"openai_api_key": SECRET}}]
}

CREDENTIAL_ENV_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DIFY_API_KEY",
    "DIFY_MODEL_CREDENTIALS",
)


def llm_workflow() -> Workflow:
    wf = Workflow("llm-app")
    start = wf.start([text_input("q")])
    llm = wf.llm(start["q"], model="langgenius/openai/openai:gpt-4o-mini", id="llm")
    answer = wf.answer(llm.output)
    wf.connect(start, llm, answer)
    return wf


class TestNoCredentialsNeeded:
    def test_a_stubbed_run_works_with_a_stripped_environment(self, monkeypatch):
        """Nothing ambient is consulted, so CI needs no secrets configured."""
        for name in CREDENTIAL_ENV_VARS:
            monkeypatch.delenv(name, raising=False)

        result = llm_workflow().run({"q": "hi"}, llm=StubLLM("ok"))
        assert result.succeeded
        assert result["answer"] == "ok"

    def test_credentials_are_not_picked_up_from_the_environment(self, monkeypatch):
        """Real keys are an explicit argument, never an ambient default.

        Reading them from the environment would quietly turn a test run into a
        billed API call, which is exactly what the stub exists to avoid.
        """
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        monkeypatch.setenv("DIFY_MODEL_CREDENTIALS", str(CREDENTIALS))

        with pytest.raises(WorkflowRunError, match="Could not build the workflow"):
            llm_workflow().run({"q": "hi"})


class TestCredentialsDoNotLeak:
    def test_a_build_failure_does_not_echo_the_credentials(self):
        with pytest.raises(WorkflowRunError) as caught:
            llm_workflow().run({"q": "hi"}, credentials=CREDENTIALS)
        assert SECRET not in str(caught.value)

    def test_the_error_names_the_way_out(self):
        with pytest.raises(WorkflowRunError, match="StubLLM"):
            llm_workflow().run({"q": "hi"})

    def test_credentials_never_reach_the_exported_document(self):
        wf = llm_workflow()
        try:
            wf.run({"q": "hi"}, credentials=CREDENTIALS)
        except WorkflowRunError:
            pass
        assert SECRET not in wf.to_yaml(include_secret=True)


class TestFailuresNameTheirOwnFix:
    """A build failure must point at the piece that was missing, not at a guess."""

    def test_missing_credentials_suggest_credentials_or_a_stub(self):
        wf = llm_workflow()
        wf.depends_on("langgenius/openai:0.3.8@abc123")
        with pytest.raises(WorkflowRunError) as caught:
            wf.run({"q": "hi"})
        message = str(caught.value)
        assert "credentials=" in message
        assert "StubLLM" in message

    def test_a_missing_plugin_suggests_depends_on(self):
        """Not 'pass credentials' — the credentials were never the problem."""
        with pytest.raises(WorkflowRunError) as caught:
            llm_workflow().run({"q": "hi"}, credentials={"model_credentials": []})
        message = str(caught.value)
        assert "depends_on" in message
        assert "credentials=" not in message

    def test_an_unclassified_failure_gets_no_misleading_hint(self):
        from graphon.dsl.errors import DslError

        from dify_client.workflow.runner import _build_failure_message

        message = _build_failure_message(
            DslError("Something else broke.", code="graph.build_failed")
        )
        assert message == "Could not build the workflow: Something else broke."
        assert "StubLLM" not in message

    def test_a_missing_plugin_runtime_is_explained(self):
        from graphon.dsl.errors import DslError

        from dify_client.workflow.runner import _build_failure_message

        message = _build_failure_message(
            DslError(
                "dify-plugin-daemon-slim is not available in PATH.",
                code="runtime.slim_unavailable",
            )
        )
        assert "StubLLM" in message


class TestBuildingNeedsNothing:
    def test_a_workflow_can_be_built_and_exported_with_no_environment(
        self, monkeypatch
    ):
        """Authoring and exporting are offline operations end to end."""
        for name in CREDENTIAL_ENV_VARS:
            monkeypatch.delenv(name, raising=False)

        text = llm_workflow().to_yaml()
        assert "langgenius/openai/openai" in text
        assert SECRET not in text
