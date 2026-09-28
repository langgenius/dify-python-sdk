"""Turning a Dify event stream into the same RunResult a local run produces."""

import json
from decimal import Decimal

from dify_client.workflow.dify_runner import _collect, _iter_events


def sse(*events: dict) -> list[str]:
    return [f"data: {json.dumps(event)}" for event in events]


NODE_FINISHED = {
    "event": "node_finished",
    "data": {
        "node_id": "llm",
        "node_type": "llm",
        "title": "LLM",
        "status": "succeeded",
        "inputs": {"#start.q#": "hello"},
        "process_data": {"model_name": "gpt-4o-mini"},
        "outputs": {"text": "hi there"},
        "elapsed_time": 1.25,
        "execution_metadata": {
            "total_tokens": 42,
            "total_price": "0.000123",
            "currency": "USD",
        },
    },
}

WORKFLOW_FINISHED = {
    "event": "workflow_finished",
    "data": {"status": "succeeded", "outputs": {"answer": "hi there"}, "error": None},
}


class TestParsingTheStream:
    def test_blank_lines_and_keepalives_are_skipped(self):
        lines = ["", "   ", "event: ping", "data: [DONE]", 'data: {"event":"x"}']
        assert [e["event"] for e in _iter_events(lines)] == ["x"]

    def test_bytes_are_accepted(self):
        assert [e["event"] for e in _iter_events([b'data: {"event":"x"}'])] == ["x"]

    def test_malformed_json_does_not_stop_the_stream(self):
        lines = ["data: {not json", 'data: {"event":"x"}']
        assert [e["event"] for e in _iter_events(lines)] == ["x"]

    def test_non_object_payloads_are_ignored(self):
        assert list(_iter_events(["data: [1,2]", 'data: {"event":"x"}'])) == [
            {"event": "x"}
        ]


class TestNodeResults:
    def test_a_finished_node_becomes_a_node_result(self):
        result = _collect(sse(NODE_FINISHED, WORKFLOW_FINISHED))
        node = result.node("llm")
        assert node.node_type == "llm"
        assert node.succeeded
        assert node.outputs == {"text": "hi there"}
        assert node.inputs == {"#start.q#": "hello"}
        assert node.process_data == {"model_name": "gpt-4o-mini"}

    def test_per_node_usage_carries_tokens_and_price(self):
        """Blocking mode reports only a token total; this is why we stream."""
        node = _collect(sse(NODE_FINISHED, WORKFLOW_FINISHED)).node("llm")
        assert node.usage.total_tokens == 42
        assert node.usage.total_price == Decimal("0.000123")
        assert node.usage.currency == "USD"
        assert node.usage.latency == 1.25

    def test_usage_sums_across_nodes(self):
        second = json.loads(json.dumps(NODE_FINISHED))
        second["data"]["node_id"] = "llm2"
        second["data"]["execution_metadata"] = {
            "total_tokens": 8,
            "total_price": "0.000077",
            "currency": "USD",
        }
        result = _collect(sse(NODE_FINISHED, second, WORKFLOW_FINISHED))
        assert result.usage.total_tokens == 50
        assert result.usage.total_price == Decimal("0.0002")

    def test_a_node_without_metadata_costs_nothing(self):
        plain = {
            "event": "node_finished",
            "data": {"node_id": "start", "node_type": "start", "status": "succeeded"},
        }
        result = _collect(sse(plain, WORKFLOW_FINISHED))
        assert not result.node("start").usage

    def test_a_zero_price_is_not_recorded_as_a_cost(self):
        free = json.loads(json.dumps(NODE_FINISHED))
        free["data"]["execution_metadata"] = {
            "total_tokens": 5,
            "total_price": 0,
            "currency": "USD",
        }
        result = _collect(sse(free, WORKFLOW_FINISHED))
        assert result.usage.total_tokens == 5
        assert result.usage.costs == {}


class TestWorkflowResult:
    def test_outputs_and_status_come_from_the_final_event(self):
        result = _collect(sse(NODE_FINISHED, WORKFLOW_FINISHED))
        assert result.succeeded
        assert result["answer"] == "hi there"

    def test_a_failed_workflow_carries_its_error(self):
        failed = {
            "event": "workflow_finished",
            "data": {"status": "failed", "outputs": {}, "error": "node llm failed"},
        }
        result = _collect(sse(failed))
        assert not result.succeeded
        assert result.error == "node llm failed"

    def test_a_stream_error_event_fails_the_run(self):
        result = _collect(
            sse({"event": "error", "data": {"message": "quota exceeded"}})
        )
        assert result.status == "failed"
        assert "quota" in result.error

    def test_streamed_text_is_collected(self):
        chunks = [
            {"event": "text_chunk", "data": {"text": "hi "}},
            {"event": "text_chunk", "data": {"text": "there"}},
        ]
        result = _collect(sse(*chunks, WORKFLOW_FINISHED))
        assert "".join(result.stream) == "hi there"

    def test_a_truncated_stream_is_not_reported_as_success(self):
        """No workflow_finished means we never learned the outcome."""
        result = _collect(sse(NODE_FINISHED))
        assert result.status == "unknown"
        assert not result.succeeded


class TestSameShapeAsALocalRun:
    def test_assertions_written_for_a_stub_run_carry_across(self):
        from dify_client.workflow import StubLLM, Workflow, text_input

        wf = Workflow("w")
        start = wf.start([text_input("q")])
        llm = wf.llm(start["q"], model="langgenius/openai/openai:gpt-4o-mini", id="llm")
        answer = wf.answer(llm.output)
        wf.connect(start, llm, answer)

        local = wf.run({"q": "hello"}, llm=StubLLM("hi there"))
        remote = _collect(sse(NODE_FINISHED, WORKFLOW_FINISHED))

        for result in (local, remote):
            assert result.succeeded
            assert result["answer"] == "hi there"
            assert result.node("llm").succeeded
