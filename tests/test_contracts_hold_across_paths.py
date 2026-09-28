"""Guarantees that have to mean the same thing however you got there.

The SDK's nouns were right and the guarantees behind them were not: the same
stream read two ways gave two answers, a cost nobody reported read as zero, and
a wait that failed returned as though it had not.
"""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from dify_client.results import WorkflowRun
from dify_client.streams import WorkflowRunStream, collect_run
from dify_client.usage import Usage

#: What Dify actually sends: the nodes carry a price, the finish event does not.
STREAM = [
    {"event": "workflow_started", "task_id": "t", "data": {"id": "r1"}},
    {
        "event": "node_finished",
        "task_id": "t",
        "data": {
            "id": "e1",
            "node_id": "llm",
            "status": "succeeded",
            "execution_metadata": {
                "total_tokens": 100,
                "total_price": "0.01",
                "currency": "USD",
            },
        },
    },
    {
        "event": "workflow_finished",
        "task_id": "t",
        "data": {"status": "succeeded", "outputs": {"a": 1}, "total_tokens": 100},
    },
]


def lines(events):
    return [f"data: {json.dumps(e)}" for e in events]


def response(events):
    return httpx.Response(
        200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events)
    )


class TestOneStreamOneAnswer:
    """There were two readers of a Dify event stream, and fixes landed in one.

    A streamed run kept its price through `WorkflowRunStream` and lost it
    through the workflow package's copy, for the same bytes.
    """

    def _both(self, events):
        from dify_client.workflow.dify_runner import _collect

        with WorkflowRunStream(response(events)) as stream:
            list(stream)
            watched = stream.get_final_run()
        return watched, collect_run(lines(events)), _collect(lines(events))

    def test_they_agree_on_the_outcome(self):
        watched, collected, legacy = self._both(STREAM)
        assert watched.status == collected.status == legacy.status == "succeeded"

    def test_they_agree_on_the_outputs(self):
        watched, collected, legacy = self._both(STREAM)
        assert watched.outputs == collected.outputs == legacy.outputs

    def test_they_agree_on_what_it_cost(self):
        watched, collected, legacy = self._both(STREAM)
        prices = {r.usage.total_price for r in (watched, collected, legacy)}
        assert prices == {Decimal("0.01")}

    def test_they_agree_on_an_unfinished_stream(self):
        """One called it "running", the other "unknown"."""
        watched, collected, legacy = self._both(STREAM[:2])
        assert watched.status == collected.status == legacy.status == "unknown"

    def test_there_is_one_implementation(self):
        import inspect

        from dify_client.workflow import dify_runner

        assert "collect_run" in inspect.getsource(dify_runner._collect)


class TestCostIsNotZeroWhenNobodySaid:
    def test_a_streamed_run_keeps_the_nodes_price(self):
        """Dify's workflow_finished has token counts and no cost, so taking it
        wholesale threw the price away on every streamed run."""
        run = collect_run(lines(STREAM))
        assert run.usage.total_price == Decimal("0.01")
        assert run.usage.total_tokens == 100

    def test_an_unwatched_run_says_the_cost_is_unknown(self):
        run = collect_run(lines([STREAM[-1]]))
        assert run.usage.total_tokens == 100
        assert not run.usage.cost_known
        assert run.usage.total_price is None

    def test_unknown_reads_differently_from_free(self):
        assert "not reported" in str(Usage(total_tokens=1))
        assert "free" in str(Usage(total_tokens=1, costs={}))

    def test_the_run_total_still_covers_what_was_not_watched(self):
        run = WorkflowRun(
            status="succeeded",
            executions=[],
            reported_usage=Usage(total_tokens=300),
        )
        assert run.usage.total_tokens == 300
        assert run.node_usage.total_tokens == 0

    def test_a_budget_cannot_pass_on_a_price_nobody_reported(self):
        from dify_client.workflow.live import BudgetExceeded, check_budget

        with pytest.raises(BudgetExceeded, match="nothing reported a price"):
            check_budget(collect_run(lines([STREAM[-1]])), "0.05")

    def test_a_budget_reads_the_price_that_was_reported(self):
        from dify_client.workflow.live import BudgetExceeded, check_budget

        with pytest.raises(BudgetExceeded, match="over the"):
            check_budget(collect_run(lines(STREAM)), "0.001")


class TestWaitingMeansWhatItSays:
    def _docs(self, status, error=""):
        from dify_client import DifyKnowledge

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "d",
                            "indexing_status": status,
                            "completed_segments": 0,
                            "total_segments": 3,
                            "error": error,
                        }
                    ]
                },
            )

        return DifyKnowledge(
            "k",
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler), base_url="https://x/v1"
            ),
        ).documents("ds")

    def _document(self):
        from dify_client.resources.knowledge import Document

        return Document(id="d", batch="b")

    def test_indexed_returns_when_it_worked(self):
        assert self._docs("completed").wait_until_indexed(self._document()).indexed

    @pytest.mark.parametrize("status", ["error", "paused"])
    def test_indexed_raises_when_it_did_not(self, status):
        """It used to return, so a caller searched a base that indexed nothing."""
        from dify_client.exceptions import APIError

        with pytest.raises(APIError, match=f"stopped at '{status}'"):
            self._docs(status).wait_until_indexed(self._document())

    @pytest.mark.parametrize("status", ["completed", "error", "paused"])
    def test_settled_returns_whatever_it_stopped_at(self, status):
        settled = self._docs(status).wait_until_settled(self._document())
        assert settled.status == status
        assert settled.finished

    def test_the_failure_carries_the_reason(self):
        from dify_client.exceptions import APIError

        with pytest.raises(APIError, match="quota exceeded"):
            self._docs("paused", "quota exceeded").wait_until_indexed(self._document())

    def test_the_failure_points_at_the_other_helper(self):
        from dify_client.exceptions import APIError

        with pytest.raises(APIError, match="wait_until_settled"):
            self._docs("error").wait_until_indexed(self._document())


class TestTheServiceApiNeedsNoWorkflowExtra:
    def test_running_through_openapi_does_not_need_graphon(self):
        """`OpenApiClient.apps.run` imported the workflow package for its event
        reader, so a plain install raised ModuleNotFoundError before sending."""
        import ast
        import inspect

        from dify_client.resources import openapi

        source = inspect.getsource(openapi)
        assert "workflow" not in source or "dify_runner" not in source

        from dify_client import openapi as client_module

        tree = ast.parse(Path(client_module.__file__).read_text())
        # `if TYPE_CHECKING` imports never execute, so they cost nothing at
        # run time — the Workflow type annotation is fine where the reader
        # was not.
        type_only = {
            id(child)
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test)
            for child in ast.walk(node)
        }
        for node in ast.walk(tree):
            if id(node) in type_only:
                continue
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "workflow" not in node.module.split(".")
