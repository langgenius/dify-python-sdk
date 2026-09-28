"""Four things a review caught, each verified against Dify's own source.

They share a shape: the SDK had a plausible reading of Dify's behaviour that
nothing contradicted, because the tests agreed with the SDK rather than with
the server. The shapes below are copied from `../dify-oss`'s response models.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from dify_client import AsyncDifyApp
from dify_client.resources.messages import _message_from_blocking
from dify_client.resources.runs import _run_from_blocking
from dify_client.results import form_tokens
from dify_client.streams import collect_run

from .test_resource_api import Dify, sse


class TestCiInstallsWhatTheTestsImport:
    """`tests/test_workflow.py` imports `dify_client.workflow`, which needs
    graphon — the `workflow` extra. A job that collects the suite without it
    fails at collection, where the error is about an import rather than about
    anything a test meant to check."""

    @staticmethod
    def _workflows():
        root = Path(__file__).parent.parent / ".github/workflows"
        return sorted(root.glob("*.yml"))

    def test_there_are_workflows_to_check(self):
        assert self._workflows()

    @pytest.mark.parametrize("name", [p.name for p in _workflows.__func__()])
    def test_a_job_that_runs_pytest_installs_the_extra(self, name):
        text = (Path(__file__).parent.parent / ".github/workflows" / name).read_text()
        if "pytest" not in text:
            pytest.skip(f"{name} runs no tests")
        assert "--extra workflow" in text, (
            f"{name} runs pytest without the workflow extra; "
            "collection will fail on `import dify_client.workflow`"
        )


class TestMessageHistoryDoesNotRepeatItself:
    """Dify's `MessageService.pagination_by_first_id` returns each page
    *ascending* and fetches what is older than the cursor. Continuing from the
    last item asks for everything older than the newest message on the page —
    most of the page again."""

    #: Oldest first, as Dify sends it.
    PAGE_ONE = {
        "data": [
            {"id": "m1", "query": "first", "answer": "a1"},
            {"id": "m2", "query": "second", "answer": "a2"},
        ],
        "has_more": True,
        "limit": 2,
    }
    PAGE_TWO = {
        "data": [{"id": "m0", "query": "older", "answer": "a0"}],
        "has_more": False,
        "limit": 2,
    }

    def _dify(self):
        pages = [self.PAGE_ONE, self.PAGE_TWO]

        def reply():
            return httpx.Response(
                200, json=pages.pop(0) if len(pages) > 1 else pages[0]
            )

        return Dify(**{"/messages": reply})

    def test_the_next_page_is_asked_for_by_the_oldest_id_on_this_one(self):
        dify = self._dify()
        with dify.app(user="alice") as app:
            page = app.chat.messages.list("c-1", limit=2)
            page.next_page()

        assert dify.calls[0]["params"].get("first_id") in (None, "")
        assert dify.calls[1]["params"]["first_id"] == "m1"

    def test_walking_every_page_does_not_repeat_a_message(self):
        dify = self._dify()
        with dify.app(user="alice") as app:
            ids = [
                message.id for message in app.chat.messages.list("c-1", limit=2).all()
            ]

        assert ids == ["m1", "m2", "m0"]
        assert len(ids) == len(set(ids))

    def test_the_async_client_continues_from_the_same_end(self):
        dify = self._dify()

        async def call():
            async with AsyncDifyApp(
                "app-key",
                user="alice",
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(dify.handler),
                    base_url="https://dify.test/v1",
                ),
            ) as app:
                page = await app.chat.messages.list("c-1", limit=2)
                await page.next_page()

        asyncio.run(call())
        assert dify.calls[1]["params"]["first_id"] == "m1"


class TestConversationsStillContinueFromTheOtherEnd:
    """The two are not the same, which is the whole point: conversations come
    newest-first and `last_id` means "after this one"."""

    PAGE = {
        "data": [{"id": "c1", "name": "one"}, {"id": "c2", "name": "two"}],
        "has_more": True,
        "limit": 2,
    }

    def test_the_next_page_is_asked_for_by_the_last_id(self):
        dify = Dify(**{"/conversations": httpx.Response(200, json=self.PAGE)})
        with dify.app(user="alice") as app:
            app.chat.conversations.list(limit=2).next_page()

        assert dify.calls[1]["params"]["last_id"] == "c2"


class TestAModeratedAnswerIsTheOneDifySaved:
    """Output moderation makes Dify emit `message_replace` carrying the whole
    replacement, and that replacement is what it stores. Ignoring the event
    handed back the rejected text."""

    STREAM = [
        {
            "event": "message",
            "task_id": "t",
            "message_id": "m",
            "answer": "my card is ",
        },
        {"event": "message", "task_id": "t", "message_id": "m", "answer": "4111 1111"},
        {
            "event": "message_replace",
            "task_id": "t",
            "message_id": "m",
            "answer": "I cannot share that.",
            "reason": "output moderation",
        },
        {"event": "message_end", "task_id": "t", "message_id": "m", "metadata": {}},
    ]

    def _final(self):
        dify = Dify(**{"/chat-messages": lambda: sse(self.STREAM)})
        with dify.app(user="alice") as app, app.chat.messages.stream("hi") as stream:
            list(stream)
            return stream.get_final_message()

    def test_the_answer_is_the_replacement(self):
        assert self._final().answer == "I cannot share that."

    def test_what_was_replaced_is_not_left_stuck_to_the_front(self):
        assert "4111" not in self._final().answer

    def test_the_reason_comes_with_it(self):
        assert self._final().replaced_reason == "output moderation"

    def test_it_is_still_a_finished_message(self):
        assert self._final().finished
        assert self._final().succeeded

    def test_an_unmoderated_answer_is_untouched(self):
        dify = Dify(
            **{
                "/chat-messages": lambda: sse(
                    [
                        {"event": "message", "task_id": "t", "answer": "hel"},
                        {"event": "message", "task_id": "t", "answer": "lo"},
                        {"event": "message_end", "task_id": "t", "metadata": {}},
                    ]
                )
            }
        )
        with dify.app(user="alice") as app, app.chat.messages.stream("hi") as stream:
            list(stream)
            message = stream.get_final_message()

        assert message.answer == "hello"
        assert message.replaced_reason == ""

    def test_the_replacement_is_not_streamed_as_another_chunk(self):
        """`text()` is what arrived; printing the replacement as a chunk would
        show the caller both answers."""
        dify = Dify(**{"/chat-messages": lambda: sse(self.STREAM)})
        with dify.app(user="alice") as app, app.chat.messages.stream("hi") as stream:
            pieces = list(stream.text())

        assert pieces == ["my card is ", "4111 1111"]


class TestAPausedRunSaysSoWhicheverWayItIsRead:
    """Dify reports `status: "paused"` on a run read back, with no forms
    attached — the forms were raised on the stream this client may never have
    watched. Reading the pause off the forms alone answered "not paused"."""

    RETRIEVED = {
        "id": "run-1",
        "workflow_id": "wf-1",
        "status": "paused",
        "outputs": {},
        "error": None,
    }
    #: A blocking run that paused, as `WorkflowPausedBlockingResponse` sends it.
    BLOCKING_PAUSED = {
        "task_id": "t-1",
        "workflow_run_id": "run-1",
        "data": {
            "id": "run-1",
            "status": "paused",
            "paused_nodes": ["approval"],
            "reasons": [
                {
                    "TYPE": "human_input",
                    "node_id": "approval",
                    "form_token": "form-abc",
                    "form_content": "Approve?",
                }
            ],
        },
    }

    def test_a_run_read_back_is_paused(self):
        dify = Dify(
            **{"/workflows/run/run-1": httpx.Response(200, json=self.RETRIEVED)}
        )
        with dify.app(user="alice") as app:
            run = app.workflows.runs.retrieve("run-1")

        assert run.paused
        assert not run.failed
        assert not run.succeeded
        assert not run.finished

    def test_a_blocking_run_that_pauses_carries_the_token_to_resume_it(self):
        dify = Dify(
            **{"/workflows/run": httpx.Response(200, json=self.BLOCKING_PAUSED)}
        )
        with dify.app(user="alice") as app:
            run = app.workflows.runs.create({"x": 1})

        assert run.paused
        assert run.pending_forms == ["form-abc"]

    def test_a_finished_run_is_not_paused(self):
        run = _run_from_blocking(
            {"task_id": "t", "data": {"id": "r", "status": "succeeded", "outputs": {}}}
        )
        assert not run.paused
        assert run.succeeded

    def test_a_chatflow_that_pauses_is_not_reported_as_finished(self):
        """`succeeded` on a message nobody has answered yet is how a paused
        chatflow got stored as a complete reply."""
        paused = {
            "event": "workflow_paused",
            "task_id": "t-1",
            "id": "m-1",
            "message_id": "m-1",
            "conversation_id": "c-1",
            "mode": "advanced-chat",
            "answer": "",
            "metadata": {},
            "created_at": 1700000000,
            "workflow_run_id": "run-1",
            "data": {
                "status": "paused",
                "paused_nodes": ["approval"],
                "reasons": [{"form_token": "form-xyz"}],
            },
        }
        message = _message_from_blocking(paused)

        assert message.paused
        assert message.pending_forms == ["form-xyz"]
        assert not message.finished
        assert not message.succeeded

    def test_an_ordinary_reply_is_still_finished(self):
        message = _message_from_blocking(
            {"message_id": "m", "answer": "hi", "metadata": {}}
        )
        assert message.finished
        assert not message.paused


class TestReadingPauseReasons:
    def test_tokens_come_out_of_the_nested_reasons(self):
        assert form_tokens({"data": {"reasons": [{"form_token": "a"}]}}) == ["a"]

    def test_several_forms_all_come_out(self):
        source = {"data": {"reasons": [{"form_token": "a"}, {"form_token": "b"}]}}
        assert form_tokens(source) == ["a", "b"]

    def test_a_reason_with_no_token_is_skipped(self):
        source = {"data": {"reasons": [{"node_id": "n"}, {"form_token": "a"}]}}
        assert form_tokens(source) == ["a"]

    def test_an_answer_with_no_reasons_has_none(self):
        assert form_tokens({"data": {"status": "succeeded"}}) == []
        assert form_tokens({}) == []
        assert form_tokens(None) == []

    def test_reasons_at_the_top_level_are_read_too(self):
        assert form_tokens({"reasons": [{"form_token": "a"}]}) == ["a"]


def test_the_stream_fixtures_are_json(request):
    """The SSE helper is shared; a fixture that is not JSON would silently
    decode to nothing and make every assertion above vacuous."""
    for event in TestAModeratedAnswerIsTheOneDifySaved.STREAM:
        assert json.loads(json.dumps(event))


class TestPausesAreTrackedByNode:
    """Dify names the node on every human-input event and the token on only
    some of them: `human_input_required.form_token` is nullable, and the
    filled and timeout events carry no token at all."""

    RAISED = {
        "event": "human_input_required",
        "task_id": "t",
        "workflow_run_id": "run-1",
        "data": {
            "form_id": "f-1",
            "node_id": "approval",
            "node_title": "Approve",
            "form_token": "tok-approval",
            "expiration_time": 1800000000,
        },
    }
    RAISED_SECOND = {
        "event": "human_input_required",
        "task_id": "t",
        "workflow_run_id": "run-1",
        "data": {
            "form_id": "f-2",
            "node_id": "review",
            "node_title": "Review",
            "form_token": "tok-review",
            "expiration_time": 1800000000,
        },
    }
    #: A form Dify means to be answered in its own UI. No token at all.
    RAISED_UI_ONLY = {
        "event": "human_input_required",
        "task_id": "t",
        "workflow_run_id": "run-1",
        "data": {
            "form_id": "f-3",
            "node_id": "sign_off",
            "node_title": "Sign off",
            "display_in_ui": True,
            "form_token": None,
            "expiration_time": 1800000000,
        },
    }
    PAUSED = {
        "event": "workflow_paused",
        "task_id": "t",
        "workflow_run_id": "run-1",
        "data": {
            "workflow_run_id": "run-1",
            "status": "paused",
            "paused_nodes": ["approval"],
            "reasons": [{"node_id": "approval", "form_token": "tok-approval"}],
            "outputs": {},
            "created_at": 1700000000,
            "elapsed_time": 0.5,
            "total_tokens": 0,
            "total_steps": 1,
        },
    }

    def _run(self, *events):
        return collect_run(f"data: {json.dumps(event)}\n" for event in events)

    def test_a_pause_with_no_token_is_still_a_pause(self):
        run = self._run(self.RAISED_UI_ONLY)

        assert run.paused
        assert run.status == "paused"
        assert run.pending_forms == []
        assert run.paused_nodes == ["sign_off"]

    def test_workflow_paused_alone_is_enough(self):
        """Reopening a stream on a paused run delivers this and nothing that
        led to it."""
        run = self._run(self.PAUSED)

        assert run.paused
        assert run.pending_forms == ["tok-approval"]

    def test_the_token_is_not_recorded_twice_when_both_events_arrive(self):
        run = self._run(self.RAISED, self.PAUSED)

        assert run.pending_forms == ["tok-approval"]

    def test_answering_one_of_two_forms_leaves_the_other(self):
        """The filled event names a node and carries no token, so clearing
        every pending form on it made the second one unreachable."""
        filled = {
            "event": "human_input_form_filled",
            "task_id": "t",
            "workflow_run_id": "run-1",
            "data": {
                "node_id": "approval",
                "node_title": "Approve",
                "rendered_content": "",
                "action_id": "ok",
                "action_text": "OK",
            },
        }
        run = self._run(self.RAISED, self.RAISED_SECOND, filled)

        assert run.pending_forms == ["tok-review"]
        assert run.paused, "the second form is still open"

    def test_answering_the_last_form_lets_the_run_move_again(self):
        filled = {
            "event": "human_input_form_filled",
            "task_id": "t",
            "data": {"node_id": "approval", "action_id": "ok"},
        }
        run = self._run(self.RAISED, filled)

        assert not run.paused
        assert run.pending_forms == []

    def test_one_form_expiring_does_not_fail_a_run_still_waiting(self):
        timeout = {
            "event": "human_input_form_timeout",
            "task_id": "t",
            "data": {"node_id": "approval", "node_title": "Approve"},
        }
        run = self._run(self.RAISED, self.RAISED_SECOND, timeout)

        assert run.paused
        assert not run.failed

    def test_the_last_form_expiring_fails_the_run(self):
        timeout = {
            "event": "human_input_form_timeout",
            "task_id": "t",
            "data": {"node_id": "approval"},
        }
        run = self._run(self.RAISED, timeout)

        assert run.failed
        assert "expired" in run.error

    def test_a_finished_run_is_not_left_paused(self):
        filled = {
            "event": "human_input_form_filled",
            "task_id": "t",
            "data": {"node_id": "approval", "action_id": "ok"},
        }
        finished = {
            "event": "workflow_finished",
            "task_id": "t",
            "data": {"status": "succeeded", "outputs": {"out": "done"}},
        }
        run = self._run(self.RAISED, filled, finished)

        assert run.succeeded
        assert not run.paused

    def test_a_tokenless_pause_says_where_to_answer_it(self):
        """`forms.submit(run)` cannot work here, and the reason is not that
        the run is unpaused."""
        from dify_client.exceptions import ValidationError
        from dify_client.resources.forms import _token

        with pytest.raises(ValidationError, match="sign_off"):
            _token(self._run(self.RAISED_UI_ONLY))

    def test_an_unpaused_run_still_says_it_is_not_waiting(self):
        from dify_client.exceptions import ValidationError
        from dify_client.resources.forms import _token

        finished = {
            "event": "workflow_finished",
            "task_id": "t",
            "data": {"status": "succeeded", "outputs": {}},
        }
        with pytest.raises(ValidationError, match="not waiting on a form"):
            _token(self._run(finished))


class TestDeployingReadsTheModeDifyReports:
    """`apps.deploy()` takes a Workflow, an Agent, or DSL as a string. A
    string has no `.mode`, so deciding from the definition sent an imported
    Agent to `/workflows/publish` — which Dify serves for `workflow` and
    `advanced-chat` only, and answers 400 for anything else.

    Chasing that down found the rest of it: an Agent is not live on import
    either. It publishes through the roster, and minting a key before that
    answers "Publish the Agent before enabling Web App or API access" — so
    `deploy(Agent(...))` never reached a usable key either, whichever way the
    definition was passed.
    """

    def _console(self, *, app_mode, publish=None):
        calls = []

        class FakeApps:
            def __init__(self, outer):
                self.outer = outer

        class Console:
            base_url = "https://dify.test/console/api"

            def _headers(self):
                return {}

            def _import_app(self, *args, **kwargs):
                calls.append(("import", args, kwargs))
                from dify_client.console import ImportResult

                return ImportResult(
                    id="imp-1", status="completed", app_id="app-1", app_mode=app_mode
                )

            # A definition object goes through `_deploy`, a DSL string through
            # `_import_app`. Both must end in the same place.
            def _deploy(self, *args, **kwargs):
                return self._import_app(*args, **kwargs)

            def _publish_workflow(self, app_id, **kwargs):
                calls.append(("publish", app_id, kwargs))
                if publish is not None:
                    raise publish
                return {"id": "v-1"}

            def _publish_agent(self, app_id):
                calls.append(("publish_agent", app_id))
                return "snapshot-1"

        return Console(), calls

    def _deploy(self, *, app_mode, definition, publish=None):
        from dify_client.resources.management import Apps, Keys

        console, calls = self._console(app_mode=app_mode, publish=publish)
        apps = Apps(console)
        apps.keys = Keys(console)
        apps.keys.create = lambda app: type("K", (), {"token": "app-key"})()
        return apps.deploy(definition), calls

    def test_an_agent_imported_from_yaml_publishes_through_the_roster(self):
        result, calls = self._deploy(
            app_mode="agent",
            definition="app:\n  mode: agent\n",
            publish=AssertionError("/workflows/publish must not be called"),
        )

        assert [call[0] for call in calls] == ["import", "publish_agent"]
        assert result.published
        assert result.has_key
        assert not result.error

    def test_an_agent_object_takes_the_same_path(self):
        """The two spellings of the same Agent must deploy the same way."""

        class FakeAgent:
            mode = "agent"

        result, calls = self._deploy(
            app_mode="agent",
            definition=FakeAgent(),
            publish=AssertionError("/workflows/publish must not be called"),
        )

        assert [call[0] for call in calls] == ["import", "publish_agent"]
        assert result.published

    def test_a_workflow_is_still_published(self):
        result, calls = self._deploy(app_mode="workflow", definition="app:\n")

        assert [call[0] for call in calls] == ["import", "publish"]
        assert result.published
        assert result.version == "v-1"

    def test_an_advanced_chat_app_is_still_published(self):
        _, calls = self._deploy(app_mode="advanced-chat", definition="app:\n")

        assert [call[0] for call in calls] == ["import", "publish"]

    def test_a_chat_app_has_no_draft_to_publish(self):
        """Its configuration is on the app itself; there is nothing to publish
        and `/workflows/publish` would refuse the mode."""
        result, calls = self._deploy(
            app_mode="chat",
            definition="app:\n",
            publish=AssertionError("/workflows/publish must not be called"),
        )

        assert [call[0] for call in calls] == ["import"]
        assert result.published

    def test_a_mode_dify_does_not_report_is_still_attempted(self):
        """Publishing and finding out beats guessing that it cannot be done."""
        _, calls = self._deploy(app_mode="", definition="app:\n")

        assert [call[0] for call in calls] == ["import", "publish"]


class TestTheAsyncFileResourceIsWhole:
    def test_a_preview_url_can_be_built_either_way(self):
        from dify_client.resources import AsyncFiles, Files

        class Client:
            base_url = "https://dify.test/v1"
            default_user = "alice"

        assert Files(Client()).preview_url("f-1") == AsyncFiles(Client()).preview_url(
            "f-1"
        )


class TestAFormTokenStaysOutOfTheNodeList:
    """`paused_nodes` and `pending_forms` answer different questions, and one
    keyed the other: a form Dify raised without naming a node was filed under
    its token, and the token then appeared in the list of nodes."""

    def test_a_pause_with_no_node_named_lists_no_node(self):
        raised = {
            "event": "human_input_required",
            "task_id": "t",
            "data": {"form_token": "tok-secret", "expiration_time": 1},
        }
        run = collect_run([f"data: {json.dumps(raised)}\n"])

        assert run.paused
        assert run.pending_forms == ["tok-secret"]
        assert run.paused_nodes == []

    def test_a_form_id_is_not_a_node_either(self):
        raised = {
            "event": "human_input_required",
            "task_id": "t",
            "data": {"form_id": "f-9", "form_token": "tok-2", "expiration_time": 1},
        }
        assert collect_run([f"data: {json.dumps(raised)}\n"]).paused_nodes == []

    def test_the_node_is_listed_when_dify_names_one(self):
        raised = {
            "event": "human_input_required",
            "task_id": "t",
            "data": {"node_id": "approval", "form_token": "t1", "expiration_time": 1},
        }
        assert collect_run([f"data: {json.dumps(raised)}\n"]).paused_nodes == [
            "approval"
        ]
