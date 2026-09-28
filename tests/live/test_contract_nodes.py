"""The node types this builder emits, against a real Dify.

A node is only "supported" once a server has accepted it. These tests import
and publish a workflow for each node type the builder gained, and read the
document back, because Dify rewrites what it does not recognise and a DSL that
imports can still be a DSL that cannot be published.

Nothing here calls a model, so the suite stays free to run.
"""

import uuid

import pytest
import yaml

from dify_client import Agent, DifyApp
from dify_client.exceptions import APIError
from dify_client.workflow import (
    DifyAgentNodeData,
    Workflow,
    action,
    declared_output,
    form_paragraph,
    form_select,
    loop_var,
    text_input,
    when,
)

from .conftest import HARNESS_PREFIX


def _named(what: str) -> str:
    return f"{HARNESS_PREFIX}-{what}-{uuid.uuid4().hex[:8]}"


def _deployed(management, wf: Workflow):
    """Import, publish and read back — the three steps that can each refuse."""
    result = management.apps.deploy(wf)
    result.raise_for_stage()
    return result, yaml.safe_load(management.apps.export(result.app_id))


class TestTheKnowledgeNode:
    def test_dify_accepts_one_pointed_at_a_real_knowledge_base(
        self, management, dataset
    ):
        """A plain dataset id imports: Dify falls back to it when it is a UUID."""
        wf = Workflow(_named("knowledge"))
        start = wf.start([text_input("q")])
        hits = wf.knowledge(start["q"], [dataset.id], top_k=2)
        wf.connect(start, hits, wf.end({"hits": hits.output}))

        result, document = _deployed(management, wf)
        try:
            node = next(
                n
                for n in document["workflow"]["graph"]["nodes"]
                if n["data"]["type"] == "knowledge-retrieval"
            )
            assert node["data"]["multiple_retrieval_config"]["top_k"] == 2
            # Dify encrypts dataset ids on export, keyed by the tenant. The
            # exported document therefore does not name the knowledge base,
            # and cannot be imported into another workspace with it intact —
            # the id decrypts to nothing there and is dropped, leaving a
            # knowledge node with no knowledge base and no error.
            (exported_id,) = node["data"]["dataset_ids"]
            assert exported_id and exported_id != dataset.id
        finally:
            management.apps.delete(result.app_id)


class TestContainerNodes:
    def test_a_body_survives_the_round_trip_inside_its_container(self, management):
        """Dify keeps a nested node only if the DSL says which container it belongs to."""
        wf = Workflow(_named("iteration"))
        start = wf.start([text_input("q")])
        listing = wf.code(
            "def main(q): return {'names': q.split(',')}",
            variables={"q": start["q"]},
            outputs={"names": "array[string]"},
        )
        with wf.iteration(listing["names"]) as each:
            greet = wf.template("Hi {{ n }}", variables={"n": each.item})
            each.returns(greet.output)
        wf.connect(start, listing, each, wf.end({"all": each.output}))

        result, document = _deployed(management, wf)
        try:
            nodes = {n["id"]: n for n in document["workflow"]["graph"]["nodes"]}
            assert nodes[greet.id]["parentId"] == each.id
            assert nodes[f"{each.id}start"]["type"] == "custom-iteration-start"
        finally:
            management.apps.delete(result.app_id)

    def test_a_loop_keeps_the_variables_it_carries(self, management):
        wf = Workflow(_named("loop"))
        start = wf.start([text_input("q")])
        with wf.loop(
            count=2,
            until=[when(start["q"], "is", "stop")],
            variables=[loop_var("tally", 0, type="number")],
        ) as body:
            bump = wf.code(
                "def main(n): return {'n': n + 1}",
                variables={"n": body.var("tally")},
                outputs={"n": "number"},
            )
            wf.connect(bump, wf.assign([(body.var("tally"), "over-write", bump["n"])]))
        wf.connect(start, body, wf.end({"tally": body.var("tally")}))

        result, document = _deployed(management, wf)
        try:
            node = next(
                n for n in document["workflow"]["graph"]["nodes"] if n["id"] == body.id
            )
            assert [v["label"] for v in node["data"]["loop_variables"]] == ["tally"]
        finally:
            management.apps.delete(result.app_id)

    def test_a_break_condition_about_the_body_survives_a_round_trip(self, management):
        """``body.until(...)`` writes the conditions after the loop node exists,
        which is the only point a condition can name a node inside it."""
        wf = Workflow(_named("loop-until"))
        start = wf.start([text_input("q")])
        with wf.loop(count=3) as body:
            step = wf.template("{{ q }}", variables={"q": start["q"]}, id="step")
            body.until([when(step["output"], "contains", "done")])
        wf.connect(start, body, wf.end({"q": start["q"]}))

        result, document = _deployed(management, wf)
        try:
            node = next(
                n for n in document["workflow"]["graph"]["nodes"] if n["id"] == body.id
            )
            (condition,) = node["data"]["break_conditions"]
            assert condition["variable_selector"] == ["step", "output"]
            assert condition["comparison_operator"] == "contains"
            assert node["data"]["loop_count"] == 3
        finally:
            management.apps.delete(result.app_id)

    def test_two_body_nodes_reading_the_item_both_run(self, management, service_api):
        """The start marker used to lead to the first body node alone, so a
        second one reading only the item never ran and each pass returned
        None for it. This runs the iteration on Dify: a template and a code
        node cost nothing."""
        wf = Workflow(_named("parallel-body"))
        start = wf.start([text_input("q")])
        split = wf.code(
            "def main(s: str) -> dict:\n    return {'l': s.split(',')}\n",
            variables={"s": start["q"]},
            outputs={"l": "array[string]"},
            id="split",
        )
        with wf.iteration(split["l"]) as each:
            a = wf.template("A{{ n }}", variables={"n": each.item}, id="a")
            b = wf.template("B{{ n }}", variables={"n": each.item}, id="b")
            joined = wf.template(
                "{{ x }}+{{ y }}", variables={"x": a.output, "y": b.output}, id="j"
            )
            each.returns(joined.output)
        wf.end({"all": each.output})

        result = management.apps.deploy(wf)
        try:
            result.raise_for_stage()
            with DifyApp(
                result.api_key, base_url=service_api, user=HARNESS_PREFIX
            ) as app:
                run = app.workflows.runs.create({"q": "x,y"})
            assert run.status == "succeeded", run.error
            assert run.outputs["all"] == ["Ax+Bx", "Ay+By"]
        finally:
            management.apps.delete(result.app_id)


class TestHumanInput:
    def test_the_timeout_arm_keeps_dify_s_name(self, management):
        """Dify's engine takes ``__timeout`` when nobody answers
        (``TIMEOUT_HANDLE`` in ``core/workflow/nodes/human_input``). A timeout
        is at least an hour away, so what is checked here is that the edge
        survives import as that handle; the name is Dify's own constant."""
        wf = Workflow(_named("timeout-arm"))
        start = wf.start([text_input("q")])
        review = wf.human_input("Ship?", actions=[action("ok", "OK")], id="review")
        wf.connect(start, review)
        wf.connect(review.case("ok"), wf.end({"o": start["q"]}, id="done"))
        wf.connect(review.timeout, wf.end({"o": start["q"]}, id="late"))

        result, document = _deployed(management, wf)
        try:
            edges = {
                (e["target"], e["sourceHandle"])
                for e in document["workflow"]["graph"]["edges"]
                if e["source"] == "review"
            }
            assert edges == {("done", "ok"), ("late", "__timeout")}
        finally:
            management.apps.delete(result.app_id)

    def test_dify_keeps_the_form_and_its_buttons(self, management):
        wf = Workflow(_named("human-input"))
        start = wf.start([text_input("q")])
        review = wf.human_input(
            "Approve?\n\n{{#$output.note#}}",
            inputs=[form_paragraph("note")],
            actions=[action("approve", "Approve", style="primary")],
        )
        wf.connect(start, review)
        wf.connect(review.case("approve"), wf.end({"note": review["note"]}))

        result, document = _deployed(management, wf)
        try:
            node = next(
                n
                for n in document["workflow"]["graph"]["nodes"]
                if n["data"]["type"] == "human-input"
            )
            assert [a["id"] for a in node["data"]["user_actions"]] == ["approve"]
            assert node["data"]["inputs"][0]["output_variable_name"] == "note"
        finally:
            management.apps.delete(result.app_id)

    def test_a_dropdown_can_be_filled_from_a_variable(self, management):
        """The option list has a ``variable`` form as well as a constant one,
        which is how a form offers what the run just found."""
        wf = Workflow(_named("form-select"))
        start = wf.start([text_input("q")])
        listed = wf.code(
            "def main(q): return {'topics': [q]}",
            variables={"q": start["q"]},
            outputs={"topics": "array[string]"},
            id="listed",
        )
        review = wf.human_input(
            "Pick one.",
            inputs=[form_select("topic", listed["topics"])],
            actions=[action("ok", "OK")],
        )
        wf.connect(listed, review)
        wf.connect(review.case("ok"), wf.end({"topic": review["topic"]}))

        result, document = _deployed(management, wf)
        try:
            node = next(
                n
                for n in document["workflow"]["graph"]["nodes"]
                if n["data"]["type"] == "human-input"
            )
            source = node["data"]["inputs"][0]["option_source"]
            assert source["type"] == "variable"
            assert source["selector"] == ["listed", "topics"]
        finally:
            management.apps.delete(result.app_id)


class TestConversationVariables:
    def test_a_chatflow_keeps_the_variables_it_declares(self, management):
        wf = Workflow(_named("conv-vars"), mode="advanced-chat")
        start = wf.start([text_input("q")])
        seen = wf.conversation_var("seen", [], type="array[string]")
        note = wf.assign([(seen, "append", start["q"])])
        wf.connect(start, note, wf.answer("done"))

        result, document = _deployed(management, wf)
        try:
            (declared,) = document["workflow"]["conversation_variables"]
            assert declared["selector"] == ["conversation", "seen"]
        finally:
            management.apps.delete(result.app_id)


@pytest.fixture
def llm_model(management):
    """A model reference this workspace can actually call."""
    names = management.models.names("llm")
    if not names:
        pytest.skip("no llm provider is configured on this Dify")
    return names[0]


@pytest.fixture
def roster_agent(management, llm_model):
    """A published Agent on the roster, which is what a binding may name."""
    agent = Agent.create(
        _named("roster-agent"),
        instruction="Decide whether an incoming message needs paging.",
        model=llm_model,
        role="Triage",
    )
    deployment = management.apps.deploy(agent)
    deployment.raise_for_stage()
    try:
        (entry,) = [
            a for a in management.agents.list() if a.name.startswith(agent.name)
        ]
        yield entry
    finally:
        management.apps.delete(deployment.app_id)


class TestAgentNodes:
    def test_a_strategy_node_publishes_before_its_plugin_is_checked(self, management):
        """Dify resolves an agent strategy when the node runs, not when it publishes.

        Worth pinning: it means a missing plugin is a run-time failure on the
        server, so publishing is not the check it looks like.
        """
        wf = Workflow(_named("agent"))
        start = wf.start([text_input("q")])
        think = wf.agent("langgenius/agent/function_calling", {"query": start["q"]})
        wf.connect(start, think, wf.end({"o": think["text"]}))

        result, _ = _deployed(management, wf)
        management.apps.delete(result.app_id)

    def test_a_roster_binding_is_turned_into_a_record_on_import(
        self, management, roster_agent
    ):
        """The binding is a server record, and naming the Agent is what creates it.

        Dify resolves the Agent while it writes the draft, so an unpublished
        or missing one fails the import rather than the publish.
        """
        wf = Workflow(_named("dify-agent"))
        start = wf.start([text_input("q")])
        node = wf.dify_agent(
            roster_agent,
            "Say whether this pages someone.",
            outputs=[declared_output("severity")],
        )
        wf.connect(start, node, wf.end({"o": start["q"]}))

        result, document = _deployed(management, wf)
        try:
            data = next(
                n
                for n in document["workflow"]["graph"]["nodes"]
                if n["data"]["type"] == "agent"
            )["data"]
            assert data["agent_binding"]["binding_type"] == "roster_agent"
        finally:
            management.apps.delete(result.app_id)

    def test_an_inline_agent_travels_inside_the_workflow(self, management, llm_model):
        """The package route: Dify creates an Agent owned by the node on import."""
        agent = Agent.create(
            _named("inline-agent"),
            instruction="Decide whether an incoming message needs paging.",
            model=llm_model,
        )
        wf = Workflow(_named("inline"))
        start = wf.start([text_input("q")])
        node = wf.inline_agent(
            agent,
            "Say whether this pages someone.",
            outputs=[declared_output("severity")],
        )
        wf.connect(start, node, wf.end({"o": start["q"]}))

        result, document = _deployed(management, wf)
        try:
            data = next(
                n
                for n in document["workflow"]["graph"]["nodes"]
                if n["data"]["type"] == "agent"
            )["data"]
            assert data["agent_binding"] == {
                "binding_type": "inline_agent",
                "package_ref": "agent_1",
            }
            # Dify hands the packaged node its job config through `agent_job`,
            # not through the loose keys a roster node uses.
            assert data["agent_job"]["workflow_prompt"] == (
                "Say whether this pages someone."
            )
            assert list(document["agent_packages"]) == ["agent_1"]
        finally:
            management.apps.delete(result.app_id)

    def test_an_agent_node_with_no_binding_at_all_is_refused_on_import(
        self, management
    ):
        """An empty binding is not a binding Dify will write a record for."""
        wf = Workflow(_named("unbound-agent"))
        start = wf.start([text_input("q")])
        agent = wf.add(DifyAgentNodeData(title="Agent"))
        wf.connect(start, agent, wf.end({"o": start["q"]}))

        result = management.apps.import_definition(wf.to_yaml())
        try:
            assert not result.imported
        finally:
            if result.app_id:
                management.apps.delete(result.app_id)

    def test_a_binding_that_names_a_kind_but_no_agent_fails_at_publish(
        self, management
    ):
        """The editor's half-built shape — and a different failure from the above.

        Import skips a binding whose ids are missing, so the draft is written
        and the refusal arrives a step later. Both helpers insist on naming an
        Agent for exactly this reason.
        """
        wf = Workflow(_named("half-bound-agent"))
        start = wf.start([text_input("q")])
        agent = wf.add(
            DifyAgentNodeData(
                title="Agent", agent_binding={"binding_type": "inline_agent"}
            )
        )
        wf.connect(start, agent, wf.end({"o": start["q"]}))

        drafted = management.apps.import_definition(wf.to_yaml())
        try:
            assert drafted.imported
            with pytest.raises(APIError, match="binding"):
                management.apps.publish(drafted.app_id)
        finally:
            management.apps.delete(drafted.app_id)


class TestPipelineNodes:
    def test_a_datasource_node_is_accepted_in_an_app_workflow(self, management):
        """Datasource and knowledge-index belong to pipelines, and an app takes them."""
        wf = Workflow(_named("datasource"))
        source = wf.datasource(plugin_id="langgenius/file", provider="file")
        wf.connect(source, wf.end({}))

        result, _ = _deployed(management, wf)
        management.apps.delete(result.app_id)

    def test_a_knowledge_index_node_is_accepted_in_an_app_workflow(self, management):
        wf = Workflow(_named("knowledge-index"))
        start = wf.start([text_input("q")])
        chunks = wf.code(
            "def main(q): return {'chunks': [q]}",
            variables={"q": start["q"]},
            outputs={"chunks": "array[string]"},
        )
        index = wf.knowledge_index(chunks["chunks"])
        wf.connect(start, chunks, index, wf.end({"o": start["q"]}))

        result, _ = _deployed(management, wf)
        management.apps.delete(result.app_id)


class TestHttpRequest:
    def test_a_json_body_carrying_a_reference_survives_the_round_trip(self, management):
        """Dify substitutes the template into the body text and parses it, so
        the reference has to arrive as the template, quoted as a string."""
        wf = Workflow(_named("http-json"))
        start = wf.start([text_input("q")])
        hook = wf.http(
            "https://example.test/hook",
            method="post",
            json={"q": start["q"]},
            id="hook",
        )
        wf.connect(hook, wf.end({"o": start["q"]}))

        result, document = _deployed(management, wf)
        try:
            node = next(
                n for n in document["workflow"]["graph"]["nodes"] if n["id"] == "hook"
            )
            body = node["data"]["body"]
            assert body["type"] == "json"
            assert body["data"][0]["value"] == '{"q": "{{#start.q#}}"}'
        finally:
            management.apps.delete(result.app_id)


class TestBranches:
    def test_a_node_after_an_arm_does_not_run_when_the_other_arm_is_taken(
        self, management, service_api
    ):
        """It also reads the start node, which used to give it an edge around
        the branch — and Dify runs a node unless every way in was skipped."""
        from dify_client.workflow import when

        wf = Workflow(_named("arm-bypass"))
        start = wf.start([text_input("q")])
        branch = wf.if_else([when(start["q"], "is", "yes")], id="br")
        esc = wf.template("E{{ q }}", variables={"q": start["q"]}, id="esc")
        que = wf.template("Q{{ q }}", variables={"q": start["q"]}, id="que")
        wf.connect(branch.true, esc)
        wf.connect(branch.false, que)
        after = wf.template(
            "{{ e }} {{ q }}", variables={"e": esc.output, "q": start["q"]}, id="after"
        )
        merged = wf.aggregate([after.output, que.output], id="merged")
        wf.end({"a": merged.output})

        result = management.apps.deploy(wf)
        try:
            result.raise_for_stage()
            with DifyApp(
                result.api_key, base_url=service_api, user=HARNESS_PREFIX
            ) as app:
                taken = app.workflows.runs.create({"q": "yes"})
                other = app.workflows.runs.create({"q": "nope"})
            assert taken.outputs["a"] == "Eyes yes"
            assert other.outputs["a"] == "Qnope"
        finally:
            management.apps.delete(result.app_id)


class TestLoopConditions:
    def test_conditions_from_both_places_survive_the_round_trip(self, management):
        wf = Workflow(_named("loop-both"))
        start = wf.start([text_input("q")])
        with wf.loop(count=2, until=[when(start["q"], "is", "stop")]) as body:
            step = wf.template("{{ q }}", variables={"q": start["q"]}, id="step")
            body.until([when(step["output"], "contains", "done")])
        wf.connect(start, body, wf.end({"q": start["q"]}))

        result, document = _deployed(management, wf)
        try:
            node = next(
                n for n in document["workflow"]["graph"]["nodes"] if n["id"] == body.id
            )
            selectors = [
                c["variable_selector"] for c in node["data"]["break_conditions"]
            ]
            assert selectors == [["start", "q"], ["step", "output"]]
        finally:
            management.apps.delete(result.app_id)


class TestWeightedMerge:
    def test_the_switch_survives_the_round_trip(self, management, dataset):
        """Running the merge needs embedded bases, which is billed; what is
        free is that Dify keeps the flag it gates the merge on."""
        from dify_client.workflow import weighted_score

        wf = Workflow(_named("weighted"))
        start = wf.start([text_input("q")])
        hits = wf.knowledge(
            start["q"],
            [dataset.id],
            weights=weighted_score(
                embedding="langgenius/openai/openai:text-embedding-3-small"
            ),
        )
        wf.connect(start, hits, wf.end({"hits": hits.output}))

        result, document = _deployed(management, wf)
        try:
            node = next(
                n
                for n in document["workflow"]["graph"]["nodes"]
                if n["data"]["type"] == "knowledge-retrieval"
            )
            config = node["data"]["multiple_retrieval_config"]
            assert config["reranking_mode"] == "weighted_score"
            assert config["reranking_enable"] is True
        finally:
            management.apps.delete(result.app_id)
