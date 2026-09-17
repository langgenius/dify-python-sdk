"""Tests for building, exporting and locally running workflows."""

import pytest
import yaml

from dify_client.workflow import (
    Workflow,
    WorkflowError,
    WorkflowRunError,
    number,
    run_dsl,
    select,
    text_input,
)


def greeter() -> Workflow:
    wf = Workflow("greeter")
    start = wf.start([text_input("name", label="Your name")])
    greet = wf.template(
        "Hello, {{ name }}!", variables={"name": start["name"]}, id="greet"
    )
    answer = wf.answer(greet.output)
    wf.connect(start, greet, answer)
    return wf


class TestReferences:
    def test_index_builds_template_and_selector(self):
        wf = Workflow("w")
        start = wf.start([text_input("name")])
        ref = start["name"]
        assert ref.template == "{{#start.name#}}"
        assert ref.selector == ["start", "name"]
        assert str(ref) == "{{#start.name#}}"

    def test_default_output_field_is_node_type_aware(self):
        wf = Workflow("w")
        tmpl = wf.template("hi", id="t")
        llm = wf.llm("hi", model="langgenius/openai/openai:gpt-4o-mini", id="l")
        assert tmpl.output.field == "output"
        assert llm.output.field == "text"


class TestBuilding:
    def test_node_ids_are_derived_and_deduplicated(self):
        wf = Workflow("w")
        wf.start()
        first = wf.template("a")
        second = wf.template("b")
        assert first.id == "template_transform"
        assert second.id == "template_transform_2"

    def test_duplicate_explicit_id_is_rejected(self):
        wf = Workflow("w")
        wf.template("a", id="same")
        with pytest.raises(WorkflowError, match="already used"):
            wf.template("b", id="same")

    def test_answer_node_makes_it_a_chatflow(self):
        assert greeter().mode == "advanced-chat"

    def test_end_node_makes_it_a_workflow(self):
        wf = Workflow("w")
        start = wf.start([text_input("name")])
        end = wf.end({"greeting": start["name"]})
        wf.connect(start, end)
        assert wf.mode == "workflow"

    def test_explicit_mode_wins(self):
        wf = Workflow("w", mode="workflow")
        wf.start()
        wf.answer("hi")
        assert wf.mode == "workflow"

    def test_llm_model_reference_is_split(self):
        wf = Workflow("w")
        llm = wf.llm("hi", model="langgenius/openai/openai:gpt-4o-mini")
        assert llm.data.model.provider == "langgenius/openai/openai"
        assert llm.data.model.name == "gpt-4o-mini"

    def test_llm_model_reference_without_model_name_is_rejected(self):
        wf = Workflow("w")
        with pytest.raises(WorkflowError, match="missing a model name"):
            wf.llm("hi", model="langgenius/openai/openai")

    def test_llm_prompt_accepts_variable_references(self):
        wf = Workflow("w")
        start = wf.start([text_input("q")])
        llm = wf.llm(
            [("system", "Be brief."), ("user", start["q"])],
            model="langgenius/openai/openai:gpt-4o-mini",
        )
        assert llm.data.prompt_template[1].text == "{{#start.q#}}"

    def test_llm_prompt_accepts_a_bare_variable_reference(self):
        """A prompt assembled by an upstream node is passed straight in."""
        wf = Workflow("w")
        tmpl = wf.template("hi", id="prompt")
        llm = wf.llm(tmpl.output, model="langgenius/openai/openai:gpt-4o-mini")
        assert llm.data.prompt_template[0].text == "{{#prompt.output#}}"
        assert str(llm.data.prompt_template[0].role) == "user"

    def test_connect_needs_two_nodes(self):
        wf = Workflow("w")
        start = wf.start()
        with pytest.raises(WorkflowError, match="at least two"):
            wf.connect(start)

    def test_escape_hatch_accepts_any_graphon_entity(self):
        from graphon.nodes.if_else.entities import IfElseNodeData

        wf = Workflow("w")
        branch = wf.add(IfElseNodeData(title="Branch"), id="branch")
        assert branch.type == "if-else"
        assert branch.id == "branch"


class TestValidation:
    def test_empty_workflow_is_rejected(self):
        with pytest.raises(WorkflowError, match="no nodes"):
            Workflow("w").to_dict()

    def test_a_workflow_with_nothing_to_start_from_is_rejected(self):
        wf = Workflow("w")
        a = wf.template("a")
        b = wf.answer("b")
        wf.connect(a, b)
        with pytest.raises(WorkflowError, match="nothing to start from"):
            wf.to_dict()

    def test_two_start_nodes_are_rejected(self):
        wf = Workflow("w")
        first = wf.start(id="start")
        second = wf.start(id="start2")
        answer = wf.answer("x")
        wf.connect(first, answer)
        wf.connect(second, answer)
        with pytest.raises(WorkflowError, match="more than one start node"):
            wf.to_dict()

    def test_chatflow_without_answer_is_rejected(self):
        wf = Workflow("w", mode="advanced-chat")
        start = wf.start()
        tmpl = wf.template("x")
        wf.connect(start, tmpl)
        with pytest.raises(WorkflowError, match="needs a 'answer' node"):
            wf.to_dict()

    def test_orphan_node_is_rejected(self):
        wf = Workflow("w")
        start = wf.start()
        answer = wf.answer("x")
        wf.connect(start, answer)
        wf.template("stranded", id="stranded")
        with pytest.raises(WorkflowError, match="not connected"):
            wf.to_dict()


class TestSerialisation:
    def test_emits_an_importable_app_document(self):
        doc = greeter().to_dict()
        assert doc["kind"] == "app"
        assert doc["version"] == "0.7.0"
        assert doc["app"]["mode"] == "advanced-chat"
        assert set(doc["workflow"]) == {
            "graph",
            "features",
            "environment_variables",
            "conversation_variables",
        }

    def test_app_block_declares_an_emoji_icon(self):
        app = greeter().to_dict()["app"]
        assert app["icon_type"] == "emoji"
        assert app["icon"]

    def test_version_stays_a_string_through_yaml(self):
        """Dify rejects a non-string version, and 0.7.0 must not become a float."""
        doc = yaml.safe_load(greeter().to_yaml())
        assert doc["version"] == "0.7.0"
        assert isinstance(doc["version"], str)

    def test_unimportable_mode_is_rejected(self):
        wf = Workflow("w", mode="chat")
        start = wf.start()
        answer = wf.answer("x")
        wf.connect(start, answer)
        with pytest.raises(WorkflowError, match="not importable as a workflow"):
            wf.to_dict()

    def test_nodes_carry_canvas_positions(self):
        graph = greeter().to_dict()["workflow"]["graph"]
        xs = [n["position"]["x"] for n in graph["nodes"]]
        assert xs == sorted(xs)
        assert len(set(xs)) == 3, "chained nodes should sit in separate columns"

    def test_edges_declare_both_node_types(self):
        graph = greeter().to_dict()["workflow"]["graph"]
        first = graph["edges"][0]
        assert first["data"]["sourceType"] == "start"
        assert first["data"]["targetType"] == "template-transform"

    def test_yaml_round_trips(self):
        wf = greeter()
        assert yaml.safe_load(wf.to_yaml()) == wf.to_dict()

    def test_to_yaml_writes_the_file(self, tmp_path):
        path = tmp_path / "greeter.yml"
        greeter().to_yaml(path)
        assert yaml.safe_load(path.read_text())["app"]["name"] == "greeter"

    def test_declared_plugin_dependencies_are_exported(self):
        wf = greeter()
        wf.depends_on("langgenius/openai:0.3.8@abc123")
        assert wf.to_dict()["dependencies"] == [
            {
                "type": "marketplace",
                "value": {
                    "marketplace_plugin_unique_identifier": "langgenius/openai:0.3.8@abc123"
                },
            }
        ]


class TestLocalRun:
    def test_runs_without_a_dify_server(self):
        result = greeter().run({"name": "Dify"})
        assert result.succeeded
        assert result["answer"] == "Hello, Dify!"

    def test_exposes_per_node_results(self):
        result = greeter().run({"name": "Dify"})
        greet = result.node("greet")
        assert greet.succeeded
        assert greet.node_type == "template-transform"
        assert greet["output"] == "Hello, Dify!"
        assert greet.inputs == {"name": "Dify"}

    def test_unknown_node_lists_the_nodes_that_ran(self):
        result = greeter().run({"name": "Dify"})
        with pytest.raises(KeyError, match="greet"):
            result.node("nope")

    def test_the_exported_dsl_is_what_runs(self):
        """A local pass is a statement about the document Dify would import."""
        dsl = greeter().to_yaml()
        assert run_dsl(dsl, inputs={"name": "Dify"})["answer"] == "Hello, Dify!"

    def test_multiple_inputs(self):
        wf = Workflow("adder")
        start = wf.start([number("a"), number("b")])
        total = wf.template(
            "{{ a }} + {{ b }}",
            variables={"a": start["a"], "b": start["b"]},
            id="total",
        )
        answer = wf.answer(total.output)
        wf.connect(start, total, answer)
        assert wf.run({"a": 1, "b": 2})["answer"] == "1 + 2"

    def test_raise_on_error_surfaces_failures(self):
        wf = Workflow("boom")
        start = wf.start([text_input("name")])
        # Jinja2 rejects an undefined filter at render time.
        bad = wf.template(
            "{{ name | no_such_filter }}", variables={"name": start["name"]}, id="bad"
        )
        answer = wf.answer(bad.output)
        wf.connect(start, bad, answer)
        with pytest.raises(WorkflowRunError):
            wf.run({"name": "x"}, raise_on_error=True)

    def test_a_failed_run_is_a_result_not_an_exception(self):
        wf = Workflow("boom")
        start = wf.start([text_input("name")])
        bad = wf.template(
            "{{ name | no_such_filter }}", variables={"name": start["name"]}, id="bad"
        )
        answer = wf.answer(bad.output)
        wf.connect(start, bad, answer)

        result = wf.run({"name": "x"})
        assert not result.succeeded
        assert result.status == "failed"
        assert "no_such_filter" in result.error

    def test_select_input_is_accepted(self):
        wf = Workflow("picker")
        start = wf.start([select("tone", ["formal", "casual"])])
        echo = wf.template("{{ tone }}", variables={"tone": start["tone"]}, id="echo")
        answer = wf.answer(echo.output)
        wf.connect(start, echo, answer)
        assert wf.run({"tone": "casual"})["answer"] == "casual"
