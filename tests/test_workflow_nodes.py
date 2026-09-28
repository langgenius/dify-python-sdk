"""The node types a workflow can be built from, and what each one refuses.

Every shape asserted here was imported into a running Dify 1.17.1 and
published before it was written down. What the tests guard is the other half:
that the builder keeps refusing the configurations Dify accepts and then
cannot run.
"""

from __future__ import annotations

import pytest
import yaml

from dify_client import Agent
from dify_client.workflow import (
    Chunk,
    StubCode,
    StubKnowledge,
    StubLLM,
    Workflow,
    WorkflowError,
    action,
    bearer,
    declared_output,
    form_paragraph,
    loop_var,
    parameter,
    text_input,
    when,
)
from dify_client.workflow.results import WorkflowRunError

MODEL = "langgenius/openai/openai:gpt-4o-mini"


def graph(wf: Workflow) -> dict:
    return yaml.safe_load(wf.to_yaml())["workflow"]["graph"]


def node_of(wf: Workflow, type_: str) -> dict:
    return next(n for n in graph(wf)["nodes"] if n["data"]["type"] == type_)


# -- knowledge retrieval ---------------------------------------------------


def test_a_knowledge_node_needs_a_knowledge_base():
    """Dify's editor refuses a knowledge node with no dataset, and so does this.

    The DSL accepts one, imports it and fails at run time with nothing to
    search, which is a long way from where the mistake was made.
    """
    wf = Workflow("kb")
    start = wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="at least one knowledge base"):
        wf.knowledge(start["q"], [])


def test_single_mode_retrieval_needs_the_model_that_picks_the_base():
    wf = Workflow("kb")
    start = wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="mode='single'"):
        wf.knowledge(start["q"], ["ds-1"], mode="single")


def test_reranking_is_only_enabled_once_a_model_is_chosen():
    """A node marked rerank-enabled with no rerank model deploys and cannot run."""
    wf = Workflow("kb")
    start = wf.start([text_input("q")])
    plain = wf.knowledge(start["q"], ["ds-1"])
    assert plain.data.multiple_retrieval_config.reranking_enable is False

    reranked = wf.knowledge(start["q"], ["ds-1"], rerank=f"{MODEL}", id="kb2")
    assert reranked.data.multiple_retrieval_config.reranking_enable is True


def _kb_workflow() -> tuple[Workflow, object]:
    wf = Workflow("kb")
    start = wf.start([text_input("q")])
    hits = wf.knowledge(start["q"], ["ds-1"], top_k=2)
    end = wf.end({"hits": hits.output})
    wf.connect(start, hits, end)
    return wf, hits


def test_a_stubbed_knowledge_node_answers_from_its_chunks():
    wf, _ = _kb_workflow()
    know = StubKnowledge(["first", "second"])
    result = wf.run({"q": "anything"}, knowledge=know)

    assert result.status == "succeeded"
    assert [hit["content"] for hit in result.outputs["hits"]] == ["first", "second"]
    assert know.calls[0].query == "anything"
    assert know.calls[0].dataset_ids == ("ds-1",)


def test_a_retrieved_chunk_carries_the_keys_dify_sends():
    """Read off a live 1.17.1 run: a workflow that indexes these locally keeps working."""
    wf, _ = _kb_workflow()
    result = wf.run({"q": "x"}, knowledge=StubKnowledge(["text"]))

    hit = result.outputs["hits"][0]
    assert set(hit) == {"content", "title", "metadata", "files", "summary"}
    assert hit["metadata"]["_source"] == "knowledge"
    assert hit["metadata"]["dataset_id"] == "ds-1"


def test_chunks_with_scores_come_back_best_first():
    wf, _ = _kb_workflow()
    know = StubKnowledge(
        [Chunk("worse", score=0.2), Chunk("better", score=0.9)],
    )
    result = wf.run({"q": "x"}, knowledge=know)

    assert [hit["content"] for hit in result.outputs["hits"]] == ["better", "worse"]


def test_chunks_without_scores_keep_the_order_the_stub_states():
    """An unscored chunk is not a chunk scored zero; reordering one hides the fixture."""
    wf, _ = _kb_workflow()
    result = wf.run({"q": "x"}, knowledge=StubKnowledge(["b", "a"]))

    assert [hit["content"] for hit in result.outputs["hits"]] == ["b", "a"]


def test_top_k_cuts_the_merged_result():
    wf, _ = _kb_workflow()
    result = wf.run({"q": "x"}, knowledge=StubKnowledge(["1", "2", "3"]))

    assert len(result.outputs["hits"]) == 2


def test_a_stub_answers_per_knowledge_base():
    wf = Workflow("kb")
    start = wf.start([text_input("q")])
    hits = wf.knowledge(start["q"], ["ds-1"], top_k=5)
    end = wf.end({"hits": hits.output})
    wf.connect(start, hits, end)

    know = StubKnowledge({"ds-1": ["mine"], "ds-2": ["not asked for"]})
    result = wf.run({"q": "x"}, knowledge=know)

    assert [hit["content"] for hit in result.outputs["hits"]] == ["mine"]


def test_running_a_knowledge_node_without_a_stub_says_what_to_pass():
    """graphon has no knowledge node, and the failure should name the way through."""
    wf, _ = _kb_workflow()
    with pytest.raises(WorkflowRunError, match="StubKnowledge"):
        wf.run({"q": "x"})


# -- conditions and branching ----------------------------------------------


def test_an_unknown_comparison_is_rejected_where_it_is_written():
    """Dify spells inequality '≠'; '!=' imports fine and the branch never fires."""
    wf = Workflow("branch")
    start = wf.start([text_input("q")])
    with pytest.raises(ValueError, match="not a comparison Dify knows"):
        when(start["q"], "!=", "x")


def test_a_comparison_that_takes_no_value_is_not_given_one():
    wf = Workflow("branch")
    start = wf.start([text_input("q")])
    with pytest.raises(ValueError, match="compares nothing"):
        when(start["q"], "empty", "")


def test_an_if_else_node_names_its_branches_after_its_cases():
    wf = Workflow("branch")
    start = wf.start([text_input("q")])
    branch = wf.if_else({"big": [when(start["q"], "is", "a")]})
    end = wf.end({"o": start["q"]})
    wf.connect(start, branch)
    wf.connect(branch, end, handle="big")

    cases = node_of(wf, "if-else")["data"]["cases"]
    assert [case["case_id"] for case in cases] == ["big"]
    handles = {e["sourceHandle"] for e in graph(wf)["edges"]}
    assert "big" in handles


def test_an_if_else_node_with_no_conditions_is_rejected():
    wf = Workflow("branch")
    wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="no conditions"):
        wf.if_else([])


def test_a_branch_takes_the_arm_its_condition_chose():
    wf = Workflow("branch")
    start = wf.start([text_input("q")])
    branch = wf.if_else([when(start["q"], "contains", "urgent")])
    hot = wf.template("HOT")
    cold = wf.template("cold")
    merged = wf.aggregate([hot.output, cold.output])
    end = wf.end({"o": merged.output})
    wf.connect(start, branch)
    wf.connect(branch, hot, handle="true")
    wf.connect(branch, cold, handle="false")
    wf.connect(hot, merged)
    wf.connect(cold, merged)
    wf.connect(merged, end)

    assert wf.run({"q": "urgent!"}).outputs["o"] == "HOT"
    assert wf.run({"q": "later"}).outputs["o"] == "cold"


def test_a_list_position_is_counted_from_one():
    """Zero used to enable extraction while silently selecting item one."""
    wf = Workflow("list")
    start = wf.start([text_input("items")])

    with pytest.raises(WorkflowError, match="counts from 1"):
        wf.list_operator(start["items"], extract=0)


# -- http ------------------------------------------------------------------


def test_headers_are_written_the_way_dify_parses_them():
    """Dify stores headers as one `name: value` per line and splits on the first colon."""
    wf = Workflow("call")
    start = wf.start([text_input("q")])
    call = wf.http(
        "https://example.com", headers={"Accept": "application/json", "X-A": "b"}
    )
    wf.connect(start, call, wf.end({"o": call.output}))

    assert node_of(wf, "http-request")["data"]["headers"] == (
        "Accept: application/json\nX-A: b"
    )


def test_a_request_has_one_body():
    wf = Workflow("call")
    wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="one body"):
        wf.http("https://example.com", json={"a": 1}, text="raw")


def test_an_http_credential_travels_in_the_config_dify_reads():
    wf = Workflow("call")
    start = wf.start([text_input("q")])
    call = wf.http("https://example.com", auth=bearer("tok"))
    wf.connect(start, call, wf.end({"o": call.output}))

    auth = node_of(wf, "http-request")["data"]["authorization"]
    assert auth == {
        "type": "api-key",
        "config": {"type": "bearer", "api_key": "tok", "header": ""},
    }


# -- model nodes -----------------------------------------------------------


def test_a_classifier_needs_something_to_choose_between():
    wf = Workflow("cls")
    start = wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="at least two classes"):
        wf.classify(start["q"], ["only one"], model=MODEL)


def test_a_classifier_keeps_the_class_ids_it_was_given():
    """The class id is the edge handle, so a generated one would break the wiring."""
    wf = Workflow("cls")
    start = wf.start([text_input("q")])
    kind = wf.classify(
        start["q"], {"refund": "a refund", "other": "anything else"}, model=MODEL
    )
    end = wf.end({"o": kind["class_name"]})
    wf.connect(start, kind)
    wf.connect(kind, end, handle="refund")

    classes = node_of(wf, "question-classifier")["data"]["classes"]
    assert [c["id"] for c in classes] == ["refund", "other"]


def test_a_parameter_extractor_with_no_parameters_is_rejected():
    wf = Workflow("pe")
    start = wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="extracts nothing"):
        wf.extract_parameters(start["q"], [], model=MODEL)


def test_a_model_reference_without_a_model_name_is_rejected():
    wf = Workflow("pe")
    start = wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="missing a model name"):
        wf.extract_parameters(
            start["q"], [parameter("a")], model="langgenius/openai/openai"
        )


# -- variables -------------------------------------------------------------


def test_a_conversation_variable_is_exported_with_the_selector_dify_reads():
    wf = Workflow("chat", mode="advanced-chat")
    start = wf.start([text_input("q")])
    seen = wf.conversation_var("seen", [], type="array[string]")
    note = wf.assign([(seen, "append", start["q"])])
    answer = wf.answer("done")
    wf.connect(start, note, answer)

    declared = yaml.safe_load(wf.to_yaml())["workflow"]["conversation_variables"]
    assert declared[0]["selector"] == ["conversation", "seen"]
    assert declared[0]["value_type"] == "array[string]"


def test_a_conversation_variable_needs_a_starting_value():
    """Dify's own factory refuses a mapping with no value, with `missing value`."""
    wf = Workflow("chat", mode="advanced-chat")
    with pytest.raises(WorkflowError, match="starting value"):
        wf.conversation_var("seen", None)


def test_an_unknown_assignment_operation_is_rejected():
    wf = Workflow("chat", mode="advanced-chat")
    seen = wf.conversation_var("seen", "")
    with pytest.raises(WorkflowError, match="not an assignment Dify knows"):
        wf.assign([(seen, "overwrite", "x")])


# -- containers ------------------------------------------------------------


def _iterating_workflow() -> tuple[Workflow, object]:
    wf = Workflow("iter")
    start = wf.start([text_input("q")])
    listing = wf.code(
        "def main(q): return {'names': q.split(',')}",
        variables={"q": start["q"]},
        outputs={"names": "array[string]"},
    )
    with wf.iteration(listing["names"]) as each:
        greet = wf.template("Hi {{ n }}", variables={"n": each.item})
        each.returns(greet.output)
    end = wf.end({"all": each.output})
    wf.connect(start, listing, each, end)
    return wf, each


def test_an_iteration_runs_its_body_once_per_element():
    wf, _ = _iterating_workflow()
    result = wf.run({"q": "a,b"}, code=StubCode({"names": ["ann", "bo"]}))

    assert result.outputs["all"] == ["Hi ann", "Hi bo"]


def test_a_nested_node_says_which_container_it_belongs_to():
    """Dify draws the body inside the box from parentId, and positions it relative to it."""
    wf, each = _iterating_workflow()
    nodes = {n["id"]: n for n in graph(wf)["nodes"]}
    body = [n for n in nodes.values() if n.get("parentId") == each.id]

    assert {n["data"]["type"] for n in body} == {
        "iteration-start",
        "template-transform",
    }
    assert all(n["extent"] == "parent" for n in body)
    assert all(n["data"]["isInIteration"] for n in body)
    assert nodes[f"{each.id}start"]["type"] == "custom-iteration-start"


def test_an_iteration_that_never_says_what_it_returns_is_rejected():
    wf = Workflow("iter")
    start = wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="what each pass returns"):
        with wf.iteration(start["q"]) as each:
            wf.template("nothing returned")
    assert each is not None


def test_a_container_with_an_empty_body_is_rejected():
    wf = Workflow("iter")
    start = wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="contains no nodes"):
        with wf.iteration(start["q"]):
            pass


def test_a_loop_carries_a_variable_between_passes():
    wf = Workflow("loop")
    start = wf.start([text_input("q")])
    with wf.loop(count=3, variables=[loop_var("tally", 0, type="number")]) as body:
        bump = wf.code(
            "def main(n): return {'n': n + 1}",
            variables={"n": body.var("tally")},
            outputs={"n": "number"},
        )
        store = wf.assign([(body.var("tally"), "over-write", bump["n"])])
        wf.connect(bump, store)
    end = wf.end({"tally": body.var("tally")})
    wf.connect(start, body, end)

    result = wf.run({"q": "x"}, code=StubCode({"n": 7}))
    assert result.status == "succeeded"
    assert result.outputs["tally"] == 7
    assert sum(1 for run in result.executions if run.node_id == bump.id) == 3


def test_a_loop_variable_must_be_declared_before_it_is_read():
    wf = Workflow("loop")
    wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="not a variable of this loop"):
        with wf.loop(count=1) as body:
            body.var("nope")


def test_an_iteration_has_an_item_and_a_loop_does_not():
    """The two read differently, and each kind is its own type.

    Reaching for the other kind's member is still named rather than left as a
    bare AttributeError, because the two used to be one class.
    """
    from dify_client.workflow import Iteration, Loop

    wf = Workflow("both")
    start = wf.start([text_input("q")])
    with wf.iteration(start["q"]) as each:
        step = wf.template("x")
        each.returns(step.output)
    assert isinstance(each, Iteration)

    with pytest.raises(WorkflowError, match="Only an iteration has item"):
        with wf.loop(count=1) as body:
            assert isinstance(body, Loop)
            body.item


# -- nodes Dify runs itself ------------------------------------------------


def test_a_human_input_node_needs_a_way_to_continue():
    wf = Workflow("form")
    wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="no way to continue"):
        wf.human_input("Approve?", actions=[])


def test_a_human_input_form_keeps_its_fields_and_buttons():
    wf = Workflow("form")
    start = wf.start([text_input("q")])
    review = wf.human_input(
        "Approve?\n\n{{#$output.note#}}",
        inputs=[form_paragraph("note")],
        actions=[action("approve", "Approve", style="primary")],
    )
    end = wf.end({"o": review["note"]})
    wf.connect(start, review)
    wf.connect(review.case("approve"), end)

    data = node_of(wf, "human-input")["data"]
    assert data["inputs"] == [{"type": "paragraph", "output_variable_name": "note"}]
    assert data["user_actions"] == [
        {"id": "approve", "title": "Approve", "button_style": "primary"}
    ]


def test_an_agent_node_declares_the_plugin_it_needs():
    wf = Workflow("agent")
    start = wf.start([text_input("q")])
    think = wf.agent(
        "langgenius/agent/function_calling",
        {"query": start["q"]},
        plugin="langgenius/agent:0.0.18@hash",
    )
    end = wf.end({"o": think["text"]})
    wf.connect(start, think, end)

    document = yaml.safe_load(wf.to_yaml())
    assert (
        document["dependencies"][0]["value"]["marketplace_plugin_unique_identifier"]
        == "langgenius/agent:0.0.18@hash"
    )
    data = node_of(wf, "agent")["data"]
    assert data["agent_strategy_provider_name"] == "langgenius/agent"
    assert data["agent_parameters"]["query"] == {
        "type": "variable",
        "value": ["start", "q"],
    }


def test_an_agent_strategy_without_a_provider_is_rejected():
    wf = Workflow("agent")
    wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="missing its provider"):
        wf.agent("function_calling")


def test_a_plugin_trigger_is_a_root_a_workflow_may_start_at():
    """A trigger takes the place of a start node; without that, validate() refuses it."""
    wf = Workflow("trig")
    trigger = wf.plugin_trigger(
        plugin_id="langgenius/github",
        provider_id="github",
        event="push",
        subscription_id="sub-1",
        plugin_unique_identifier="langgenius/github:0.0.1@hash",
    )
    end = wf.end({"o": trigger["ref"]})
    wf.connect(trigger, end)

    assert node_of(wf, "trigger-plugin")["data"]["event_name"] == "push"


def test_stubbing_a_model_still_works_beside_the_new_nodes():
    """The stubs compose: one run may stub the model, the sandbox and retrieval."""
    wf = Workflow("mix")
    start = wf.start([text_input("q")])
    hits = wf.knowledge(start["q"], ["ds-1"])
    reply = wf.llm(f"Use {hits.output}", model=MODEL)
    end = wf.end({"o": reply.output})
    wf.connect(start, hits, reply, end)

    result = wf.run(
        {"q": "x"},
        llm=StubLLM("answered"),
        knowledge=StubKnowledge(["a fact"]),
    )
    assert result.outputs["o"] == "answered"


def test_an_agent_node_binds_the_roster_agent_it_names():
    """The binding is what Dify turns into a record while importing the draft."""
    wf = Workflow("agent")
    start = wf.start([text_input("q")])
    node = wf.dify_agent("agent-123", "Decide.", outputs=[declared_output("severity")])
    wf.connect(start, node, wf.end({"o": start["q"]}))

    data = node_of(wf, "agent")["data"]
    assert data["agent_binding"] == {
        "binding_type": "roster_agent",
        "agent_id": "agent-123",
    }
    assert data["agent_task"] == "Decide."
    assert data["agent_declared_outputs"][0]["name"] == "severity"
    assert data["version"] == "2"


def test_an_agent_node_needs_an_agent_to_bind():
    """An unbound node imports or publishes and then refuses; catch it here."""
    wf = Workflow("agent")
    wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="roster Agent id"):
        wf.dify_agent("")


def test_an_inline_agent_is_exported_as_a_package_the_node_refers_to():
    wf = Workflow("agent")
    start = wf.start([text_input("q")])
    agent = Agent("researcher", soul={"schema_version": 1, "prompt": {}})
    node = wf.inline_agent(agent, "Summarise.", outputs=[declared_output("gist")])
    wf.connect(start, node, wf.end({"o": start["q"]}))

    document = yaml.safe_load(wf.to_yaml())
    data = node_of(wf, "agent")["data"]
    assert data["agent_binding"] == {
        "binding_type": "inline_agent",
        "package_ref": "agent_1",
    }
    # A packaged node's job config is read from `agent_job`, not from the two
    # loose keys a roster node uses.
    assert data["agent_job"]["workflow_prompt"] == "Summarise."
    assert document["agent_packages"]["agent_1"]["metadata"]["name"] == "researcher"


def test_an_inline_agent_is_blanked_by_export_like_any_other_secret():
    """to_yaml() is committable, so the agent's credentials go the way env vars do."""
    wf = Workflow("agent")
    start = wf.start([text_input("q")])
    agent = Agent(
        "researcher",
        soul={
            "schema_version": 1,
            "model": {"model": "x", "credential_ref": "cred-1"},
        },
    )
    node = wf.inline_agent(agent)
    wf.connect(start, node, wf.end({"o": start["q"]}))

    exported = yaml.safe_load(wf.to_yaml())["agent_packages"]["agent_1"]
    kept = yaml.safe_load(wf.to_yaml(include_secret=True))["agent_packages"]["agent_1"]
    assert exported["soul"]["model"]["credential_ref"] is None
    assert kept["soul"]["model"]["credential_ref"] == "cred-1"


def test_an_inline_agent_takes_an_agent_not_an_id():
    """The two routes bind different things; a swapped argument is a wrong node."""
    wf = Workflow("agent")
    wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="dify_client.agent.Agent"):
        wf.inline_agent("agent-123")


def test_a_datasource_is_a_root_a_workflow_may_start_at():
    wf = Workflow("pipeline")
    source = wf.datasource(plugin_id="langgenius/file", provider="file")
    wf.connect(source, wf.end({}))

    assert node_of(wf, "datasource")["data"]["provider_name"] == "file"


def test_a_knowledge_index_node_points_at_the_chunks_it_writes():
    wf = Workflow("pipeline")
    start = wf.start([text_input("q")])
    chunks = wf.code("def main(): return {'c': []}", outputs={"c": "array[string]"})
    index = wf.knowledge_index(chunks["c"])
    wf.connect(start, chunks, index, wf.end({"o": start["q"]}))

    data = node_of(wf, "knowledge-index")["data"]
    assert data["index_chunk_variable_selector"] == chunks["c"].selector
    assert data["chunk_structure"] == "text_model"
    assert data["indexing_technique"] == "economy"


def test_a_chunk_structure_dify_does_not_index_is_rejected():
    """The DSL takes any string and the pipeline then has nothing to write."""
    wf = Workflow("pipeline")
    start = wf.start([text_input("q")])
    chunks = wf.code("def main(): return {'c': []}", outputs={"c": "array[string]"})
    with pytest.raises(WorkflowError, match="not one Dify indexes"):
        wf.knowledge_index(chunks["c"], structure="general_structure")
    assert start is not None


def test_a_loop_can_be_left_from_inside_it():
    """`until` is checked between passes; a loop-end node leaves during one."""
    wf = Workflow("loop")
    start = wf.start([text_input("q")])
    with wf.loop(count=5) as body:
        step = wf.code("def main(): return {'n': 1}", outputs={"n": "number"})
        check = wf.if_else([when(step["n"], "=", "1")])
        wf.connect(step, check)
        wf.connect(check, body.stop(), handle="true")
    wf.connect(start, body, wf.end({"o": start["q"]}))

    result = wf.run({"q": "x"}, code=StubCode({"n": 1}))
    assert result.status == "succeeded"
    assert sum(1 for run in result.executions if run.node_id == step.id) == 1


def test_an_iteration_cannot_be_left_early():
    wf = Workflow("iter")
    start = wf.start([text_input("q")])
    with pytest.raises(WorkflowError, match="Only a loop has stop"):
        with wf.iteration(start["q"]) as each:
            each.stop()
