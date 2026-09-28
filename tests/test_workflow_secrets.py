"""The exported DSL must be safe to commit.

Dify's own exporter blanks SecretVariable values unless the caller explicitly
asks for them; these tests hold this SDK to the same default, and guard the
whole document against picking up credential material later.
"""

import pytest
import yaml

from dify_client.workflow import StubLLM, Workflow, WorkflowError, text_input

SECRET = "t0p-s3cret-value-0000"
MODEL_KEY = "sk-model-key-should-never-be-exported"


def workflow_with_a_secret() -> Workflow:
    wf = Workflow("secrets-demo")
    token = wf.env_var("API_TOKEN", SECRET, secret=True)
    region = wf.env_var("REGION", "apac")
    start = wf.start([text_input("q")])
    body = wf.template(
        "{{ tok }} in {{ reg }}",
        variables={"tok": token, "reg": region},
        id="body",
    )
    answer = wf.answer(body.output)
    wf.connect(start, body, answer)
    return wf


class TestDeclaring:
    def test_an_env_var_is_referenced_through_the_env_namespace(self):
        wf = Workflow("w")
        ref = wf.env_var("REGION", "apac")
        assert ref.template == "{{#env.REGION#}}"
        assert ref.selector == ["env", "REGION"]

    def test_a_duplicate_name_is_rejected(self):
        wf = Workflow("w")
        wf.env_var("REGION", "apac")
        with pytest.raises(WorkflowError, match="already declared"):
            wf.env_var("REGION", "emea")

    def test_value_types_are_inferred(self):
        wf = Workflow("w")
        wf.env_var("REGION", "apac")
        wf.env_var("RETRIES", 3)
        wf.env_var("TOKEN", "x", secret=True)
        assert [v.value_type for v in wf.env_vars] == ["string", "number", "secret"]

    def test_a_secret_does_not_render_itself(self):
        wf = Workflow("w")
        wf.env_var("TOKEN", SECRET, secret=True)
        assert SECRET not in repr(wf.env_vars[0])
        assert "<secret>" in repr(wf.env_vars[0])


class TestExport:
    def test_a_secret_value_is_blanked_by_default(self):
        doc = workflow_with_a_secret().to_dict()
        by_name = {v["name"]: v for v in doc["workflow"]["environment_variables"]}
        assert by_name["API_TOKEN"]["value"] == ""
        assert by_name["API_TOKEN"]["value_type"] == "secret"

    def test_a_plain_value_survives(self):
        doc = workflow_with_a_secret().to_dict()
        by_name = {v["name"]: v for v in doc["workflow"]["environment_variables"]}
        assert by_name["REGION"]["value"] == "apac"

    def test_the_secret_appears_nowhere_in_the_yaml(self):
        assert SECRET not in workflow_with_a_secret().to_yaml()

    def test_the_secret_appears_nowhere_in_the_written_file(self, tmp_path):
        path = tmp_path / "wf.yml"
        workflow_with_a_secret().to_yaml(path)
        assert SECRET not in path.read_text(encoding="utf-8")

    def test_include_secret_is_an_explicit_opt_in(self):
        text = workflow_with_a_secret().to_yaml(include_secret=True)
        assert SECRET in text

    def test_the_variable_is_still_declared_when_blanked(self):
        """Importing the DSL must still create the variable, just without a value."""
        doc = workflow_with_a_secret().to_dict()
        names = [v["name"] for v in doc["workflow"]["environment_variables"]]
        assert names == ["API_TOKEN", "REGION"]


class TestLocalRunsStillSeeTheValue:
    def test_a_run_uses_the_real_secret(self):
        """The run happens in memory, so it gets what the export omits."""
        result = workflow_with_a_secret().run({"q": "x"})
        assert result["answer"] == f"{SECRET} in apac"

    def test_running_does_not_change_what_gets_exported(self):
        wf = workflow_with_a_secret()
        wf.run({"q": "x"})
        assert SECRET not in wf.to_yaml()


class TestNothingElseLeaks:
    def test_model_credentials_never_reach_the_document(self):
        """Credentials are passed to the run, never serialised into the DSL."""
        wf = Workflow("llm-app")
        start = wf.start([text_input("q")])
        llm = wf.llm(start["q"], model="langgenius/openai/openai:gpt-4o-mini", id="llm")
        answer = wf.answer(llm.output)
        wf.connect(start, llm, answer)

        wf.run({"q": "hi"}, llm=StubLLM("ok"))
        text = wf.to_yaml()
        assert MODEL_KEY not in text
        assert "api_key" not in text
        assert "credentials" not in text

    def test_the_document_carries_no_value_outside_what_was_declared(self):
        """A blanket guard: every string in the export is one we put there."""
        doc = workflow_with_a_secret().to_dict()
        flat = yaml.safe_dump(doc)
        for forbidden in (SECRET, MODEL_KEY, "sk-", "Bearer "):
            assert forbidden not in flat
