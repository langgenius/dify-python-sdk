"""States that were squashed into one, and the wrong answers that produced.

A review found each of these. They share a shape: two independent facts folded
into a single value, so one could stand in for the other.
"""

import json

import httpx
import pytest

from dify_client import DifyApp, DifyManagement, Stage
from dify_client.lifecycle import Deployment
from dify_client.results import WorkflowRun
from dify_client.streams import MessageStream, WorkflowRunStream
from dify_client.workflow import Workflow, text_input


def sse(*events):
    return httpx.Response(
        200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events)
    )


def workflow():
    wf = Workflow("w")
    start = wf.start([text_input("a")])
    wf.connect(start, wf.end({"o": start["a"]}))
    return wf


def management(handler):
    return DifyManagement(
        token="ey.T",
        base_url="https://dify.test",
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://dify.test/console/api",
        ),
    )


def console(*, import_status="completed", app_id="a1"):
    def handler(request):
        path = request.url.path.replace("/console/api", "")
        if path == "/apps/imports":
            return httpx.Response(
                200,
                json={
                    "id": "i1",
                    "status": import_status,
                    "app_id": app_id if import_status == "completed" else None,
                    "app_mode": "workflow",
                    "error": "" if import_status == "completed" else "bad DSL",
                },
            )
        if path.endswith("/publish"):
            return httpx.Response(200, json={"id": "version-1"})
        if path.endswith("/api-keys"):
            return httpx.Response(200, json={"id": "k1", "token": "app-minted"})
        return httpx.Response(200, json={})

    return handler


class TestDeploymentFactsAreIndependent:
    """`stage` was a ladder, so a later rung stood in for an earlier one."""

    def test_a_failed_import_over_an_existing_app_is_not_runnable(self):
        """The app id came from the argument rather than the reply, so the
        deploy carried on and published whatever draft was already there."""
        result = management(console(import_status="failed")).apps.deploy(
            "kind: app", app_id="existing"
        )

        assert not result.imported
        assert not result.published
        assert not result.runnable
        assert result.stage is Stage.NOT_IMPORTED
        assert "bad DSL" in result.error

    def test_a_200_with_a_failed_status_is_still_a_failure(self):
        result = management(console(import_status="failed")).apps.deploy(workflow())
        assert not result.imported

    def test_not_publishing_does_not_report_as_published(self):
        """Minting a key used to push the stage past publish."""
        result = management(console()).apps.deploy(workflow(), publish=False)

        assert result.imported
        assert not result.published
        assert result.has_key
        assert not result.runnable
        assert result.stage is Stage.DRAFTED

    def test_published_without_a_key_is_its_own_state(self):
        result = management(console()).apps.deploy(workflow(), key=False)

        assert result.published
        assert not result.has_key
        assert result.stage is Stage.PUBLISHED

    def test_a_lost_answer_is_unknown_not_failed(self):
        """Whether Dify created the app cannot be told, and guessing is wrong."""

        def unreachable(request):
            raise httpx.ReadTimeout("no answer")

        result = management(unreachable).apps.deploy(workflow())

        assert result.indeterminate
        assert result.stage is Stage.UNKNOWN
        assert not result.imported

    def test_the_unknown_case_says_to_go_and_look(self):
        def unreachable(request):
            raise httpx.ConnectError("gone")

        result = management(unreachable).apps.deploy(workflow())
        with pytest.raises(Exception, match="never arrived"):
            result.raise_for_stage()

    def test_the_api_key_is_not_in_the_repr(self):
        """Printing a deploy result used to put a key in the log."""
        result = Deployment(imported=True, published=True, api_key="app-SECRET")
        assert "app-SECRET" not in repr(result)
        assert result.api_key == "app-SECRET"


class TestAMessageIsNotFinishedUntilDifySaysSo:
    def test_a_stream_cut_short_is_not_a_success(self):
        with MessageStream(sse({"event": "message", "answer": "par"})) as stream:
            list(stream)
            message = stream.get_final_message()

        assert message.answer == "par"
        assert not message.finished
        assert not message.succeeded

    def test_a_completed_stream_is(self):
        with MessageStream(
            sse(
                {"event": "message", "answer": "done"},
                {"event": "message_end", "metadata": {}},
            )
        ) as stream:
            list(stream)
            message = stream.get_final_message()

        assert message.finished
        assert message.succeeded

    def test_a_blocking_reply_is_finished_by_definition(self):
        """It arrives only once Dify has the whole thing."""
        from dify_client.resources.messages import _message_from_blocking

        message = _message_from_blocking({"answer": "hi", "message_id": "m"})
        assert message.finished
        assert message.succeeded

    def test_answering_a_form_stops_it_being_paused(self):
        """A resumed message reported paused forever, so it never succeeded."""
        with MessageStream(
            sse(
                {"event": "human_input_required", "data": {"form_token": "tok"}},
                {"event": "human_input_form_filled", "data": {"form_token": "tok"}},
                {"event": "message", "answer": "done"},
                {"event": "message_end", "metadata": {}},
            )
        ) as stream:
            list(stream)
            message = stream.get_final_message()

        assert not message.paused
        assert message.succeeded

    def test_a_form_that_expired_fails_rather_than_waiting(self):
        with WorkflowRunStream(
            sse(
                {"event": "workflow_started", "data": {"id": "r"}},
                {"event": "human_input_required", "data": {"form_token": "tok"}},
                {"event": "human_input_form_timeout", "data": {"form_token": "tok"}},
            )
        ) as stream:
            list(stream)
            run = stream.get_final_run()

        assert not run.paused
        assert run.failed

    def test_a_snapshot_is_a_different_question_from_the_result(self):
        with MessageStream(
            sse(
                {"event": "message", "answer": "Hel"},
                {"event": "message", "answer": "lo"},
                {"event": "message_end", "metadata": {}},
            )
        ) as stream:
            events = iter(stream)
            next(events)
            partial = stream.snapshot()
            assert partial.answer == "Hel"
            assert not partial.finished

            list(events)
            assert stream.get_final_message().finished


class TestUsageThatWasNotWatched:
    def test_reconnecting_to_a_finished_run_reports_what_it_cost(self):
        """Dify sends workflow_finished alone, with no node events. Summing
        executions called a 123-token run free."""
        with WorkflowRunStream(
            sse(
                {
                    "event": "workflow_finished",
                    "data": {
                        "id": "r1",
                        "status": "succeeded",
                        "outputs": {},
                        "total_tokens": 123,
                        "elapsed_time": 2.0,
                    },
                }
            )
        ) as stream:
            list(stream)
            run = stream.get_final_run()

        assert run.usage.total_tokens == 123

    def test_what_was_observed_is_still_separately_available(self):
        with WorkflowRunStream(
            sse(
                {
                    "event": "workflow_finished",
                    "data": {"status": "succeeded", "total_tokens": 123},
                }
            )
        ) as stream:
            list(stream)
            run = stream.get_final_run()

        assert run.node_usage.total_tokens == 0

    def test_a_blocking_run_does_not_invent_a_nameless_node(self):
        """The total used to be smuggled in as an execution with no node id,
        which then showed up in anything iterating executions."""
        from dify_client.resources.runs import _run_from_blocking

        run = _run_from_blocking(
            {
                "task_id": "t",
                "data": {"id": "r", "status": "succeeded", "total_tokens": 50},
            }
        )

        assert run.usage.total_tokens == 50
        assert run.executions == []

    def test_a_stubbed_run_still_reports_nothing(self):
        assert WorkflowRun(status="succeeded").usage.total_tokens == 0

    def test_node_totals_are_used_when_dify_gave_no_overall_figure(self):
        with WorkflowRunStream(
            sse(
                {
                    "event": "node_finished",
                    "data": {
                        "node_id": "llm",
                        "status": "succeeded",
                        "execution_metadata": {"total_tokens": 30},
                    },
                },
                {"event": "workflow_finished", "data": {"status": "succeeded"}},
            )
        ) as stream:
            list(stream)
            run = stream.get_final_run()

        assert run.usage.total_tokens == 30


class TestCallsThatDidNotExist:
    def test_run_logs_reaches_dify(self):
        """It called a method that had been deleted, so it raised AttributeError."""
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            return httpx.Response(200, json={"data": [{"id": "log-1"}]})

        app = DifyApp(
            "k",
            user="alice",
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler), base_url="https://x/v1"
            ),
        )
        assert list(app.workflows.runs.logs()) == [{"id": "log-1"}]
        assert seen["path"].endswith("/workflows/logs")

    def test_every_public_method_is_callable(self):
        """A method naming something deleted is not found until it is called."""
        import inspect

        app = DifyApp("k", base_url="https://x/v1")
        groups = [
            app.chat.messages,
            app.chat.conversations,
            app.workflows.runs,
            app.completions,
            app.files,
            app.annotations,
            app.audio,
            app.forms,
        ]
        for group in groups:
            for name in dir(group):
                if name.startswith("_"):
                    continue
                attribute = getattr(group, name)
                assert callable(attribute), f"{group}.{name}"
                inspect.signature(attribute)
