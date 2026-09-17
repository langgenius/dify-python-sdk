"""The examples must keep working.

Documentation that has drifted is worse than none, so every example's workflow
is built and — where it costs nothing — run here. Nothing in this file reaches
Dify or a model.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from dify_client.workflow import StubLLM

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def load(name: str):
    path = EXAMPLES / name
    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ALL_EXAMPLES = [
    "01_hello_workflow.py",
    "02_testing_a_prompt.py",
    "03_branching.py",
    "04_http_request.py",
    "05_deploy_and_run.py",
    "06_code_node.py",
    "08_webhook_trigger.py",
]

# 07 keeps an Agent rather than a Workflow, so the shared checks do not apply.


@pytest.mark.parametrize("name", ALL_EXAMPLES)
class TestEveryExample:
    def test_it_imports(self, name):
        assert load(name).build is not None

    def test_its_workflow_is_valid(self, name):
        load(name).build().validate()

    def test_it_exports_an_importable_document(self, name):
        doc = yaml.safe_load(load(name).build().to_yaml())
        assert doc["kind"] == "app"
        assert doc["app"]["mode"] in {"workflow", "advanced-chat"}
        assert doc["workflow"]["graph"]["nodes"]

    def test_every_model_node_declares_its_plugin(self, name):
        """An undeclared provider deploys into an app that cannot run."""
        assert load(name).build().missing_plugin_dependencies() == []


class TestHelloWorkflow:
    def test_the_style_reaches_the_template(self):
        wf = load("01_hello_workflow.py").build()
        assert (
            wf.run({"name": "Dify", "style": "formal"})["answer"]
            == "Good evening, Dify."
        )
        assert wf.run({"name": "Dify", "style": "casual"})["answer"] == "Hey Dify!"


class TestTestingAPrompt:
    def test_the_tone_reaches_the_prompt(self):
        module = load("02_testing_a_prompt.py")
        stub = StubLLM("ok")
        module.build().run({**module.INPUTS, "tone": "casual"}, llm=stub)
        assert "casual tone" in str(stub.calls[0][0].content)

    def test_a_stubbed_run_costs_nothing(self):
        module = load("02_testing_a_prompt.py")
        assert not module.build().run(module.INPUTS, llm=StubLLM("ok")).usage


class TestBranching:
    @pytest.mark.parametrize(
        ("message", "taken", "skipped"),
        [
            ("the payments API is down", "escalate", "queue"),
            ("there is an outage", "escalate", "queue"),
            ("urgent: please look", "escalate", "queue"),
            ("please review when you can", "queue", "escalate"),
        ],
    )
    def test_the_expected_path_runs(self, message, taken, skipped):
        result = (
            load("03_branching.py")
            .build()
            .run({"message": message}, raise_on_error=True)
        )
        assert taken in result.nodes
        assert skipped not in result.nodes

    def test_the_aggregator_gives_one_answer_whichever_way_it_went(self):
        """Without it, the unexecuted branch's reference leaks into the text."""
        wf = load("03_branching.py").build()
        for message in ("server down", "just a question"):
            answer = wf.run({"message": message}, raise_on_error=True)["answer"]
            assert "{{#" not in answer
            assert ".output" not in answer


class TestDeployAndRun:
    def test_it_stops_before_spending_when_the_gate_is_closed(
        self, monkeypatch, capsys
    ):
        monkeypatch.delenv("DIFY_LIVE_TESTS", raising=False)
        assert load("05_deploy_and_run.py").main() == 0
        assert "stopping before the billed half" in capsys.readouterr().out


class TestCodeNodeExample:
    def test_a_stub_answers_without_running_the_code(self):
        from dify_client.workflow import StubCode

        wf = load("06_code_node.py").build()
        result = wf.run({"text": "a b c"}, code=StubCode({"words": 3, "longest": "a"}))
        assert result.node("stats")["words"] == 3

    def test_the_local_sandbox_runs_the_real_logic(self):
        """Asked before running, not caught after: the engine turns a sandbox
        that will not start into a *node failure*, so `except
        SandboxUnavailable` here never fired and the test failed on any host
        without bubblewrap."""
        from dify_client.workflow import LocalSandbox

        if not LocalSandbox.available():
            pytest.skip("this host cannot confine code (needs macOS or bubblewrap)")

        wf = load("06_code_node.py").build()
        result = wf.run(
            {"text": "the migration failed"},
            code=LocalSandbox(),
            raise_on_error=True,
        )
        assert result.node("stats")["words"] == 3
        assert result.node("stats")["longest"] == "migration"


class TestAgentExample:
    def test_it_runs_without_anything_configured(self, monkeypatch, capsys):
        monkeypatch.delenv("DIFY_CONSOLE_TOKEN", raising=False)
        monkeypatch.delenv("DIFY_AGENT_APP_ID", raising=False)
        load("07_agent_as_code.py").main()
        assert "using a stored export" in capsys.readouterr().out

    def test_the_stored_export_reads_as_an_agent(self):
        from dify_client import Agent

        agent = Agent.from_yaml(load("07_agent_as_code.py").EXPORTED)
        assert agent.name == "support-triage"
        # The real field is `model`, not `name` — checked against a running Dify.
        assert agent.soul["model"]["model"] == "gpt-4o-mini"
        assert agent.soul["model"]["plugin_id"] == "langgenius/openai"

    def test_its_export_carries_no_credentials(self):
        from dify_client import Agent

        agent = Agent.from_yaml(load("07_agent_as_code.py").EXPORTED)
        agent.soul["model"]["credential_ref"] = "cred-SHOULD-NOT-SHIP"
        assert "cred-SHOULD-NOT-SHIP" not in agent.to_yaml()


class TestHttpExample:
    def test_it_builds_without_touching_the_network(self):
        """Building and exporting never makes a request; only run() does."""
        wf = load("04_http_request.py").build()
        assert "example.com" not in wf.to_yaml()
        assert "{{#start.url#}}" in wf.to_yaml()


class TestWebhookTrigger:
    """08 keeps two builds of one body: only the start-node one runs locally."""

    def test_the_testable_build_runs_the_shared_body(self):
        result = (
            load("08_webhook_trigger.py")
            .build_for_testing()
            .run({"order_id": "A-42", "total": "1980"}, raise_on_error=True)
        )
        assert result["line"] == "order A-42 for 1980"

    def test_the_deployable_build_starts_at_the_trigger(self):
        wf = load("08_webhook_trigger.py").build()
        assert [n.type for n in wf.nodes][0] == "trigger-webhook"
        assert not any(n.type == "start" for n in wf.nodes)

    def test_both_builds_end_the_same_way(self):
        module = load("08_webhook_trigger.py")
        outputs = [
            {o["variable"] for o in build().nodes[-1].data.model_dump()["outputs"]}
            for build in (module.build, module.build_for_testing)
        ]
        assert outputs[0] == outputs[1] == {"line"}
