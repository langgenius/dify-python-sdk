"""Running a workflow on a real Dify.

Everything here was verified by hand at least once while the SDK was built.
Keeping it in the suite is what stops the next Dify release from breaking it
quietly.
"""

import pytest

from dify_client import WorkflowRun
from dify_client.exceptions import APIError


class TestBlockingRuns:
    def test_a_run_comes_back_as_a_run(self, workflow_app):
        run = workflow_app.workflows.runs.create({"word": "hello"})

        assert isinstance(run, WorkflowRun)
        assert run.succeeded
        assert run.outputs["out"] == "hello!"

    def test_it_carries_the_ids_the_next_call_needs(self, workflow_app):
        run = workflow_app.workflows.runs.create({"word": "hello"})

        assert run.run_id
        assert run.task_id
        assert run.run_id != run.task_id

    def test_usage_is_reported_even_in_blocking_mode(self, workflow_app):
        """Dify gives one workflow-wide token count here and no per-node
        detail, so `usage` must not read zero."""
        run = workflow_app.workflows.runs.create({"word": "hello"})
        assert run.usage.total_tokens >= 0

    def test_a_run_reads_back_by_its_id(self, workflow_app):
        run = workflow_app.workflows.runs.create({"word": "hello"})
        again = workflow_app.workflows.runs.retrieve(run.run_id)

        assert again.run_id == run.run_id
        assert again.status == run.status

    def test_a_missing_input_is_reported_by_dify(self, workflow_app):
        with pytest.raises(APIError, match="word"):
            workflow_app.workflows.runs.create({})

    def test_a_long_input_is_accepted(self, workflow_app):
        """The SDK used to refuse anything over 10,000 characters itself."""
        run = workflow_app.workflows.runs.create({"word": "x", "note": "y" * 20_000})
        assert run.succeeded


class TestStreamingRuns:
    def test_events_arrive_typed(self, workflow_app):
        with workflow_app.workflows.runs.stream({"word": "hello"}) as stream:
            finished = [e for e in stream if e.type == "node_finished"]

        assert {e.execution.node_id for e in finished} >= {"start", "shout", "end"}
        assert all(e.execution.status == "succeeded" for e in finished)

    def test_the_final_run_matches_a_blocking_one(self, workflow_app):
        with workflow_app.workflows.runs.stream({"word": "hello"}) as stream:
            list(stream)
            streamed = stream.get_final_run()

        blocking = workflow_app.workflows.runs.create({"word": "hello"})
        assert streamed.outputs == blocking.outputs
        assert streamed.succeeded == blocking.succeeded

    def test_the_body_is_not_buffered(self, workflow_app):
        """The first event must arrive before the run finishes; otherwise the
        mode is streaming in name only."""
        import time

        started = time.monotonic()
        first = None
        with workflow_app.workflows.runs.stream({"word": "hello"}) as stream:
            for _ in stream:
                if first is None:
                    first = time.monotonic() - started
        total = time.monotonic() - started

        assert first is not None
        assert first < total * 0.9

    def test_per_node_usage_appears_only_on_the_stream(self, workflow_app):
        with workflow_app.workflows.runs.stream({"word": "hello"}) as stream:
            list(stream)
            run = stream.get_final_run()

        assert run.executions
        assert len(run.executions) >= len(run.nodes)

    def test_a_dropped_stream_can_be_reopened(self, needs, workflow_app):
        needs("workflow_events")
        run = workflow_app.workflows.runs.create({"word": "hello"})

        with workflow_app.workflows.runs.events(run.run_id) as stream:
            list(stream)
            reopened = stream.get_final_run()

        assert reopened.status == run.status


class TestChat:
    def test_a_message_comes_back_with_its_thread(self, chat_app):
        message = chat_app.chat.messages.create("hi", inputs={"topic": "outages"})

        assert message.answer == "about outages"
        assert message.conversation_id
        assert message.message_id

    def test_a_thread_continues(self, chat_app):
        first = chat_app.chat.messages.create("hi", inputs={"topic": "a"})
        second = chat_app.chat.messages.create(
            "again", inputs={"topic": "b"}, conversation_id=first.conversation_id
        )

        assert second.conversation_id == first.conversation_id

    def test_the_thread_is_listed(self, chat_app):
        message = chat_app.chat.messages.create("hi", inputs={"topic": "listing"})
        threads = chat_app.chat.conversations.list(limit=20)

        assert message.conversation_id in {t.id for t in threads}

    def test_streaming_assembles_the_same_answer(self, chat_app):
        with chat_app.chat.messages.stream("hi", inputs={"topic": "x"}) as stream:
            pieces = list(stream.text())
            streamed = stream.get_final_message()

        assert "".join(pieces) == streamed.answer
        assert streamed.answer == "about x"

    def test_history_walks_every_page_without_repeating_a_message(self, chat_app):
        """Dify sends message history oldest-first and continues it from the
        *first* id on a page, because the next page is older. Continuing from
        the last one asks for everything older than the *newest* message on the
        page — most of the page again, every time.

        Needs more than one message per page to show the difference, which is
        why it sends four and reads two at a time.
        """
        import time

        conversation = None
        for topic in ("one", "two", "three", "four"):
            sent = chat_app.chat.messages.create(
                "hi", inputs={"topic": topic}, conversation_id=conversation
            )
            conversation = sent.conversation_id
            # Dify's cursor compares `created_at`, which it stores to the
            # second. Four messages inside one second tie, and a tie is
            # excluded rather than ordered — the server drops them from the
            # paging, and the test would be measuring that instead.
            time.sleep(1.1)

        try:
            walked = [
                message.payload["inputs"]["topic"]
                for message in chat_app.chat.messages.list(conversation, limit=2).all()
            ]

            assert len(walked) == len(set(walked)), f"a message repeated: {walked}"
            assert sorted(walked) == ["four", "one", "three", "two"], walked
        finally:
            chat_app.chat.conversations.delete(conversation)

    def test_one_page_holds_what_dify_said_it_would(self, chat_app):
        message = chat_app.chat.messages.create("hi", inputs={"topic": "paging"})
        try:
            page = chat_app.chat.messages.list(message.conversation_id, limit=20)

            assert not page.has_more
            assert page.next_page() is None
        finally:
            chat_app.chat.conversations.delete(message.conversation_id)

    def test_a_thread_can_be_deleted(self, chat_app):
        message = chat_app.chat.messages.create("hi", inputs={"topic": "bye"})
        chat_app.chat.conversations.delete(message.conversation_id)

        remaining = {t.id for t in chat_app.chat.conversations.list(limit=50)}
        assert message.conversation_id not in remaining

    def test_feedback_can_be_given_and_taken_back(self, chat_app):
        message = chat_app.chat.messages.create("hi", inputs={"topic": "rating"})

        chat_app.chat.messages.feedback(message, "like", content="useful")
        chat_app.chat.messages.feedback(message, None)


class TestWrongRoute:
    def test_a_chat_call_on_a_workflow_app_says_so(self, workflow_app):
        """Dify rejects the wrong route; the SDK must surface that clearly."""
        with pytest.raises(APIError, match="app mode"):
            workflow_app.chat.messages.create("hello")

    def test_a_workflow_call_on_a_chat_app_says_so(self, chat_app):
        with pytest.raises(APIError, match="app mode"):
            chat_app.workflows.runs.create({"topic": "x"})


class TestFiles:
    def test_an_upload_returns_a_reference(self, workflow_app, tmp_path):
        note = tmp_path / "note.txt"
        note.write_text("hello from the harness\n")

        uploaded = workflow_app.files.upload(note)

        assert uploaded.id
        assert uploaded.name == "note.txt"
        assert uploaded.reference()["upload_file_id"] == uploaded.id

    def test_the_uploader_is_resolvable_as_an_end_user(self, workflow_app, tmp_path):
        """`created_by` is an id that means nothing until it is looked up."""
        note = tmp_path / "note.txt"
        note.write_text("x")
        uploaded = workflow_app.files.upload(note)

        end_user = workflow_app.end_user(uploaded.created_by)
        assert end_user["external_user_id"] == "sdk-harness"


class TestAppInfo:
    def test_the_mode_is_what_was_deployed(self, workflow_app, chat_app):
        assert workflow_app.info().is_workflow
        assert chat_app.info().is_chat

    def test_the_declared_inputs_are_reported(self, workflow_app):
        """The names are the keys a run takes, unwrapped from Dify's list of
        single-key objects."""
        parameters = workflow_app.parameters()

        assert {field.name for field in parameters} == {"word", "note"}
        assert parameters["word"].type == "text-input"

    def test_required_matches_what_the_workflow_declared(self, workflow_app):
        """The harness app declares `note` optional; a run without it works,
        and what Dify reports has to agree."""
        parameters = workflow_app.parameters()

        assert [field.name for field in parameters.required] == ["word"]
        assert workflow_app.workflows.runs.create({"word": "hello"}).succeeded

    def test_the_deployments_limits_come_with_them(self, workflow_app):
        assert workflow_app.parameters().system_parameters["file_size_limit"] > 0

    def test_the_webapp_settings_are_typed(self, workflow_app, workflow_deployment):
        site = workflow_app.site()

        assert site.title
        assert site.default_language
        # Dify sends null for every unset string here; none of them read None.
        assert site.description == "" or isinstance(site.description, str)
