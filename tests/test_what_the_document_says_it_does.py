"""Places where the SDK promised one thing and wrote another.

A review found each of these, and they share a shape: a docstring describing
behaviour the code did not have, or a setting accepted and then dropped. Both
fail the same way — the workflow imports, publishes, and runs as something
other than what was written.
"""

from __future__ import annotations

import pytest

from dify_client.agent import Agent, AgentError
from dify_client.workflow import (
    NodeError,
    Workflow,
    WorkflowError,
    form_paragraph,
    form_select,
    text_input,
    when,
)
from dify_client.workflow.recipes import GROUNDING, grounded_answer

MODEL = "langgenius/openai/openai:gpt-4o-mini"


def chatflow(name="w"):
    wf = Workflow(name)
    wf.start([text_input("q")])
    return wf


class TestANodeStandsForItsOutputEverywhere:
    """``Text`` accepts a node, so ``wf.answer(node)`` renders its output. An
    f-string is the one place the SDK cannot intercept, and rendering the id
    there put the word "llm" into the answer a user reads."""

    def test_a_node_in_an_f_string_is_its_output(self):
        wf = chatflow()
        reply = wf.llm("hi", model=MODEL, id="llm")

        assert f"{reply}" == "{{#llm.text#}}"

    def test_the_answer_node_renders_the_same_either_way(self):
        wf = chatflow()
        reply = wf.llm("hi", model=MODEL, id="llm")

        assert wf.answer(f"Reply: {reply}", id="a1").data.answer == (
            "Reply: {{#llm.text#}}"
        )
        assert wf.answer(reply, id="a2").data.answer == "{{#llm.text#}}"

    def test_a_node_with_an_unconventional_output_uses_it(self):
        wf = chatflow()
        hits = wf.knowledge(wf.nodes[0]["q"], ["ds"], id="hits")

        assert f"{hits}" == "{{#hits.result#}}"


class TestAnInlineAgentIsCheckedBeforeItShips:
    """The two things Dify will not accept from an Agent were checked in
    ``to_dict()``, which a workflow shipping an inline Agent never calls."""

    def agent_flow(self, *agents):
        wf = chatflow()
        previous = wf.nodes[0]
        for agent in agents:
            node = wf.inline_agent(agent, "do it")
            wf.connect(previous, node)
            previous = node
        wf.connect(previous, wf.answer("done"))
        return wf

    def test_a_nameless_inline_agent_is_refused(self):
        wf = self.agent_flow(Agent("", soul={"schema_version": 1}))

        with pytest.raises(WorkflowError, match="needs a name"):
            wf.to_dict()

    def test_an_empty_inline_agent_is_refused(self):
        wf = self.agent_flow(Agent("scout", soul={}))

        with pytest.raises(WorkflowError, match="empty soul"):
            wf.to_dict()

    def test_the_refusal_names_the_binding_that_is_wrong(self):
        wf = self.agent_flow(
            Agent("scout", soul={"schema_version": 1}),
            Agent("", soul={"schema_version": 1}),
        )

        with pytest.raises(WorkflowError, match="'agent_2'"):
            wf.to_dict()

    def test_a_good_inline_agent_still_ships(self):
        wf = self.agent_flow(Agent("scout", soul={"schema_version": 1}))

        assert wf.to_dict()["agent_packages"]["agent_1"]["metadata"]["name"] == "scout"

    def test_an_agent_app_is_refused_the_same_way(self):
        with pytest.raises(AgentError, match="needs a name"):
            Agent("", soul={"schema_version": 1}).to_dict()


class TestConversationVariablesNeedAConversation:
    """Dify keeps these on the conversation, so a ``workflow`` app has nowhere
    to put them: it imports, publishes and loses them."""

    def test_a_declared_workflow_refuses_one_when_it_is_declared(self):
        wf = Workflow("w", mode="workflow")

        with pytest.raises(WorkflowError, match="no conversation"):
            wf.conversation_var("seen", "")

    def test_an_undeclared_one_is_refused_when_the_document_is_finished(self):
        """The mode is not known while the document is being written: an app
        becomes a chatflow at ``wf.answer(...)``, which is usually written
        after the variables it reads."""
        wf = Workflow("w")
        wf.conversation_var("seen", "")
        start = wf.start([text_input("q")])
        wf.end({"o": start["q"]})

        with pytest.raises(WorkflowError, match="no conversation"):
            wf.to_dict()

    def test_a_chatflow_keeps_them(self):
        wf = chatflow()
        seen = wf.conversation_var("seen", "")
        wf.connect(wf.nodes[0], wf.answer(seen))

        assert wf.to_dict()["workflow"]["conversation_variables"][0]["name"] == "seen"


class TestSingleModeRetrievalHasNoSettingsToSet:
    """A single-mode node carries no retrieval config, so ``top_k`` and
    ``score_threshold`` were written nowhere and the search ran with the
    knowledge base's own settings."""

    @pytest.mark.parametrize(
        "given",
        [{"top_k": 9}, {"score_threshold": 0.5}, {"top_k": 9, "score_threshold": 0.5}],
    )
    def test_settings_single_mode_drops_are_refused(self, given):
        wf = chatflow()
        with pytest.raises(WorkflowError, match="mode='single' does not have"):
            wf.knowledge(wf.nodes[0]["q"], ["ds"], mode="single", model=MODEL, **given)

    def test_the_refusal_names_what_was_passed(self):
        wf = chatflow()
        with pytest.raises(WorkflowError, match="score_threshold is a setting"):
            wf.knowledge(
                wf.nodes[0]["q"],
                ["ds"],
                mode="single",
                model=MODEL,
                score_threshold=0.5,
            )

    def test_single_mode_without_them_is_fine(self):
        wf = chatflow()
        node = wf.knowledge(wf.nodes[0]["q"], ["ds"], mode="single", model=MODEL)

        assert node.data.multiple_retrieval_config is None

    def test_multiple_mode_still_defaults_top_k(self):
        wf = chatflow()
        node = wf.knowledge(wf.nodes[0]["q"], ["ds"])

        assert node.data.multiple_retrieval_config.top_k == 4

    def test_multiple_mode_still_takes_them(self):
        wf = chatflow()
        node = wf.knowledge(wf.nodes[0]["q"], ["ds"], top_k=9, score_threshold=0.5)

        assert node.data.multiple_retrieval_config.top_k == 9
        assert node.data.multiple_retrieval_config.score_threshold == 0.5


class TestASelectFieldCanBeFilledAtRunTime:
    """The docstring said to pass a variable and the code always wrote a
    constant, so ``form_select("topic", found["topics"])`` offered the two
    words of the selector as the choices."""

    def test_a_reference_builds_the_options_at_run_time(self):
        wf = chatflow()
        found = wf.llm("list topics", model=MODEL, id="found")
        field = form_select("topic", found["topics"])

        assert field.option_source.type == "variable"
        assert list(field.option_source.selector) == ["found", "topics"]

    def test_a_list_is_still_a_list_of_choices(self):
        field = form_select("size", ["small", "large"])

        assert field.option_source.type == "constant"
        assert field.option_source.value == ["small", "large"]

    def test_one_string_is_not_a_list_of_letters(self):
        with pytest.raises(NodeError, match="not a list of choices"):
            form_select("size", "small")

    def test_a_paragraph_default_takes_a_reference_too(self):
        wf = chatflow()
        draft = wf.llm("write", model=MODEL, id="draft")
        field = form_paragraph("note", default=draft["text"])

        assert field.default.type == "variable"
        assert list(field.default.selector) == ["draft", "text"]


class TestAGroundingPromptIsNotAFormatString:
    """``str.format`` on caller text meant an instruction that showed the model
    a JSON example raised ``KeyError`` on the example's own braces."""

    def test_a_brace_in_the_instruction_survives(self):
        wf = chatflow()
        node = grounded_answer(
            wf,
            wf.nodes[0]["q"],
            dataset="ds",
            model=MODEL,
            instruction='Answer as {"ok": true}.\n{context}\n{question}',
        )

        assert '{"ok": true}' in node.data.prompt_template[0].text

    def test_both_markers_are_replaced(self):
        wf = chatflow()
        node = grounded_answer(wf, wf.nodes[0]["q"], dataset="ds", model=MODEL)
        text = node.data.prompt_template[0].text

        assert "{{#start.q#}}" in text
        assert "{{#knowledge_retrieval.result#}}" in text
        assert "{context}" not in text

    def test_an_instruction_that_never_grounds_is_refused(self):
        wf = chatflow()
        with pytest.raises(WorkflowError, match="never says where"):
            grounded_answer(
                wf, wf.nodes[0]["q"], dataset="ds", model=MODEL, instruction="Answer."
            )

    def test_the_default_still_carries_both(self):
        assert "{context}" in GROUNDING
        assert "{question}" in GROUNDING


class TestALoopBreaksOnSomethingInItsBody:
    """``until=`` is evaluated before the block runs, so the only conditions it
    could name were ones outside the loop. The docstring showed otherwise, and
    the example it showed raised ``NameError``."""

    def test_a_condition_about_the_body_is_set_from_inside_it(self):
        wf = chatflow()
        with wf.loop(count=5) as body:
            guess = wf.llm("Try again", model=MODEL, id="guess")
            body.until([when(guess["text"], "contains", "done")])

        conditions = body.data.break_conditions
        assert [c.variable_selector for c in conditions] == [["guess", "text"]]

    def test_the_count_still_bounds_it(self):
        wf = chatflow()
        with wf.loop(count=3) as body:
            guess = wf.llm("Try again", model=MODEL, id="guess")
            body.until([when(guess["text"], "contains", "done")])

        assert body.data.loop_count == 3

    def test_the_logical_operator_can_be_set_with_them(self):
        wf = chatflow()
        with wf.loop(count=3) as body:
            guess = wf.llm("Try again", model=MODEL, id="guess")
            body.until(
                [
                    when(guess["text"], "contains", "done"),
                    when(guess["text"], "contains", "ok"),
                ],
                logical="or",
            )

        assert body.data.logical_operator == "or"

    def test_the_whole_document_still_renders(self):
        wf = chatflow()
        with wf.loop(count=2) as body:
            guess = wf.llm("Try again", model=MODEL, id="guess")
            body.until([when(guess["text"], "contains", "done")])
        wf.connect(wf.nodes[0], body)
        wf.connect(body, wf.answer("done"))

        wf.to_dict()


class TestANodeIsAReferenceWhereverOneIsTaken:
    """``Text`` took a bare node and the selector-taking arguments did not, so
    ``wf.end({"answer": reply})`` — the line written right after ``reply =
    wf.llm(...)`` — raised ``AttributeError: 'Node' object has no attribute
    'selector'``."""

    def reply(self):
        wf = chatflow()
        return wf, wf.llm("hi", model=MODEL, id="reply")

    def test_end(self):
        wf, reply = self.reply()
        node = wf.end({"answer": reply})
        assert node.data.outputs[0].value_selector == ["reply", "text"]

    def test_knowledge(self):
        wf, reply = self.reply()
        node = wf.knowledge(reply, ["ds"])
        assert node.data.query_variable_selector == ["reply", "text"]

    def test_classify(self):
        wf, reply = self.reply()
        node = wf.classify(reply, ["a", "b"], model=MODEL)
        assert node.data.query_variable_selector == ["reply", "text"]

    def test_aggregate(self):
        wf, reply = self.reply()
        assert wf.aggregate([reply]).data.variables == [["reply", "text"]]

    def test_extract_text(self):
        wf, _ = self.reply()
        files = wf.template("x", id="files")
        node = wf.extract_text(files)
        assert node.data.variable_selector == ["files", "output"]

    def test_list_operator(self):
        wf, _ = self.reply()
        listed = wf.template("x", id="listed")
        assert wf.list_operator(listed).data.variable == ["listed", "output"]

    def test_when(self):
        _, reply = self.reply()
        assert when(reply, "contains", "x").variable_selector == ["reply", "text"]

    def test_returns(self):
        wf, _ = self.reply()
        with wf.iteration(wf.nodes[0]["q"]) as each:
            each.returns(wf.template("{{ n }}", variables={"n": each.item}, id="t"))
        assert each.data.output_selector == ["t", "output"]

    def test_a_loop_variable_started_from_a_node_is_a_variable(self):
        """Before, only a ``VarRef`` counted as a variable, so a node was
        written as a constant."""
        from dify_client.workflow import loop_var

        _, reply = self.reply()
        var = loop_var("seen", reply)
        assert var.value_type == "variable"
        assert var.value == ["reply", "text"]

    def test_anything_else_names_the_argument_and_the_fix(self):
        wf, _ = self.reply()
        with pytest.raises(TypeError, match=r"outputs\['x'\] takes a reference"):
            wf.end({"x": 42})


class TestAJsonBodyCanCarryAReference:
    """Every other text argument of the HTTP node went through ``render()``;
    ``json=`` went through ``json.dumps`` alone, so posting a variable — the
    obvious use — raised ``TypeError: Object of type VarRef is not JSON
    serializable``."""

    def test_a_reference_is_written_as_the_template(self):
        wf = chatflow()
        node = wf.http(
            "https://example.test/hook", method="post", json={"q": wf.nodes[0]["q"]}
        )
        assert node.data.body.data[0].value == '{"q": "{{#start.q#}}"}'

    def test_a_node_stands_for_its_output(self):
        wf = chatflow()
        reply = wf.llm("hi", model=MODEL, id="reply")
        node = wf.http("https://example.test/hook", method="post", json={"a": [reply]})
        assert node.data.body.data[0].value == '{"a": ["{{#reply.text#}}"]}'

    def test_something_that_is_not_json_is_still_refused(self):
        wf = chatflow()
        with pytest.raises(TypeError, match="not JSON serializable"):
            wf.http("https://example.test/hook", method="post", json={"a": object()})
