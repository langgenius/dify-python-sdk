"""Chatflows: system variables, and the route the Service API serves them at."""

import pytest

from dify_client.workflow import (
    StubLLM,
    SystemVariables,
    VarRef,
    Workflow,
    system,
    text_input,
)
from dify_client.workflow.dify_runner import CHAT_MODES, run_on_dify


class TestSystemVariables:
    def test_it_builds_a_sys_reference(self):
        assert system.query.template == "{{#sys.query#}}"
        assert system.query.selector == ["sys", "query"]

    def test_any_name_works(self):
        """The set differs by app mode and Dify version, so nothing is fixed."""
        assert system.whatever.selector == ["sys", "whatever"]
        assert system["dialogue_count"].selector == ["sys", "dialogue_count"]

    def test_it_matches_a_hand_built_reference(self):
        assert system.query == VarRef("sys", "query")

    def test_private_names_are_not_variables(self):
        with pytest.raises(AttributeError):
            system._private

    def test_it_reads_as_itself(self):
        assert repr(system) == "system"
        assert isinstance(system, SystemVariables)

    def test_it_flows_into_a_template(self):
        wf = Workflow("w")
        start = wf.start([text_input("style")])
        echo = wf.template("You said {{ q }}", variables={"q": system.query}, id="echo")
        wf.connect(start, echo, wf.answer(echo.output))
        assert list(echo.data.variables[0].value_selector) == ["sys", "query"]


def chatflow() -> Workflow:
    wf = Workflow("chat")
    start = wf.start([text_input("style")])
    reply = wf.template("You said {{ q }}", variables={"q": system.query}, id="reply")
    wf.connect(start, reply, wf.answer(reply.output))
    return wf


class TestRouting:
    def test_an_answer_node_makes_it_a_chatflow(self):
        assert chatflow().mode == "advanced-chat"
        assert chatflow().mode in CHAT_MODES

    def test_an_end_node_keeps_it_a_workflow(self):
        wf = Workflow("w")
        start = wf.start([text_input("q")])
        wf.connect(start, wf.end({"out": start["q"]}))
        assert wf.mode == "workflow"
        assert wf.mode not in CHAT_MODES

    def test_a_chatflow_without_a_query_is_refused_before_the_request(self):
        """The Service API rejects the wrong route outright; say so first."""
        with pytest.raises(ValueError, match="query="):
            run_on_dify({"style": "formal"}, api_key="app-x", mode="advanced-chat")

    def test_the_refusal_explains_where_the_query_goes(self):
        with pytest.raises(ValueError, match="sys.query"):
            run_on_dify({}, api_key="app-x", mode="advanced-chat")

    @pytest.mark.parametrize("mode", ["advanced-chat", "chat", "agent-chat"])
    def test_every_chat_mode_needs_a_query(self, mode):
        with pytest.raises(ValueError, match="query="):
            run_on_dify({}, api_key="app-x", mode=mode)


class TestLocalRunsAreUnaffected:
    def test_a_chatflow_still_runs_locally_without_a_query(self):
        """Locally the message is just another start input."""
        result = chatflow().run({"style": "formal", "query": "hello"})
        assert result.succeeded
        assert result["answer"] == "You said hello"

    def test_a_stub_still_answers_the_model(self):
        wf = Workflow("chat-llm")
        start = wf.start([text_input("style")])
        llm = wf.llm(
            system.query, model="langgenius/openai/openai:gpt-4o-mini", id="llm"
        )
        wf.connect(start, llm, wf.answer(llm.output))
        assert wf.run({"query": "hi"}, llm=StubLLM("ok"))["answer"] == "ok"
