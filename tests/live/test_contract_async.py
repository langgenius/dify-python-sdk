"""The async surface, against a real Dify.

The sync path was verified by hand many times over; the async one was not, and
a gap in it — no way to stop a stream you started — went unnoticed until a
review read the two side by side.
"""

import pytest

from dify_client import AsyncDifyApp, AsyncPage, Message, WorkflowRun


class TestItMatchesTheSyncResult:
    async def test_a_run_comes_back_the_same(self, async_workflow_app, workflow_app):
        asynchronous = await async_workflow_app.workflows.runs.create({"word": "hello"})
        synchronous = workflow_app.workflows.runs.create({"word": "hello"})

        assert isinstance(asynchronous, WorkflowRun)
        assert asynchronous.outputs == synchronous.outputs
        assert asynchronous.succeeded == synchronous.succeeded

    async def test_a_message_comes_back_the_same(self, async_chat_app, chat_app):
        asynchronous = await async_chat_app.chat.messages.create(
            "hi", inputs={"topic": "parity"}
        )
        synchronous = chat_app.chat.messages.create("hi", inputs={"topic": "parity"})

        assert isinstance(asynchronous, Message)
        assert asynchronous.answer == synchronous.answer
        assert asynchronous.finished == synchronous.finished


class TestStreaming:
    async def test_the_answer_arrives_in_pieces(self, async_chat_app):
        stream = await async_chat_app.chat.messages.stream(
            "hi", inputs={"topic": "streaming"}
        )
        pieces = [piece async for piece in stream.text()]
        message = stream.get_final_message()

        assert "".join(pieces) == message.answer
        assert message.finished

    async def test_node_events_arrive(self, async_workflow_app):
        stream = await async_workflow_app.workflows.runs.stream({"word": "hello"})
        nodes = [e.execution.node_id async for e in stream if e.type == "node_finished"]
        run = stream.get_final_run()

        assert "shout" in nodes
        assert run.succeeded

    async def test_a_stream_can_be_abandoned_and_closed(self, async_workflow_app):
        stream = await async_workflow_app.workflows.runs.stream({"word": "hello"})
        async for _ in stream:
            break
        await stream.aclose()


class TestTheCallsThatFinishAThing:
    """Async could start a stream but not stop it, and could not answer a form,
    so a paused or long-running call had no way out."""

    async def test_a_conversation_can_be_listed_and_deleted(self, async_chat_app):
        message = await async_chat_app.chat.messages.create(
            "hi", inputs={"topic": "cleanup"}
        )
        threads = await async_chat_app.chat.conversations.list(limit=50)
        assert message.conversation_id in {t.id for t in threads}

        await async_chat_app.chat.conversations.delete(message.conversation_id)
        remaining = await async_chat_app.chat.conversations.list(limit=50)
        assert message.conversation_id not in {t.id for t in remaining}

    async def test_history_comes_back_with_the_questions(self, async_chat_app):
        first = await async_chat_app.chat.messages.create(
            "what is the refund window?", inputs={"topic": "refunds"}
        )
        page = await async_chat_app.chat.messages.list(first.conversation_id)

        assert page
        assert page[0].query == "what is the refund window?"

    async def test_feedback_can_be_given(self, async_chat_app):
        message = await async_chat_app.chat.messages.create(
            "hi", inputs={"topic": "rating"}
        )
        await async_chat_app.chat.messages.feedback(message, "like")
        await async_chat_app.chat.messages.feedback(message, None)

    async def test_a_file_uploads(self, async_workflow_app, tmp_path):
        note = tmp_path / "note.txt"
        note.write_text("async harness")

        uploaded = await async_workflow_app.files.upload(note)
        assert uploaded.id
        assert uploaded.reference()["upload_file_id"] == uploaded.id


class TestErrorsMatch:
    async def test_the_wrong_route_says_the_same_thing(self, async_workflow_app):
        from dify_client.exceptions import APIError

        with pytest.raises(APIError, match="app mode"):
            await async_workflow_app.chat.messages.create("hello")

    async def test_a_bad_key_raises_rather_than_returning(self, service_api):
        from dify_client.exceptions import AuthenticationError

        async with AsyncDifyApp(
            "app-INVALID", base_url=service_api, user="sdk-harness"
        ) as app:
            with pytest.raises(AuthenticationError):
                await app.info()


class TestTheAppsOwnFacts:
    """`parameters`, `site`, `models` and the rest existed on the sync client
    only, so an async caller had to reach into `_inner` or keep a second
    client just to read them."""

    async def test_it_reports_what_dify_is(self, async_workflow_app):
        info = await async_workflow_app.server_info()
        assert info.server_version

    async def test_it_probes_without_a_credential(self, host):
        assert (await AsyncDifyApp.probe(f"{host}/v1")).server_version

    async def test_the_declared_inputs_come_back(self, async_workflow_app):
        parameters = await async_workflow_app.parameters()
        assert {field.name for field in parameters} == {"word", "note"}

    async def test_the_webapp_settings_come_back(self, async_workflow_app):
        assert (await async_workflow_app.site()).title

    async def test_the_display_metadata_comes_back(self, async_workflow_app):
        assert isinstance(await async_workflow_app.meta(), dict)

    async def test_the_ratings_are_a_page(self, async_chat_app):
        message = await async_chat_app.chat.messages.create(
            "hi", inputs={"topic": "ratings"}
        )
        await async_chat_app.chat.messages.feedback(message, "like")

        page = await async_chat_app.feedbacks()
        assert isinstance(page, AsyncPage)
        rated = {str(f.get("message_id")) async for f in page.all()}
        assert message.message_id in rated

    async def test_open_checks_the_app_before_handing_it_back(
        self, workflow_deployment, service_api
    ):
        app = await AsyncDifyApp.open(
            workflow_deployment.api_key, base_url=service_api, user="sdk-harness"
        )
        try:
            assert (await app.info()).mode == "workflow"
        finally:
            await app.aclose()


class TestKnowledgeWorksAsynchronously:
    """There was no async knowledge client at all: anything touching a dataset
    had to be sync, in a codebase that was otherwise not."""

    async def test_a_dataset_is_created_and_deleted(self, async_knowledge):
        import uuid

        dataset = await async_knowledge.datasets.create(
            f"sdk-harness-dataset-{uuid.uuid4().hex[:8]}"
        )
        try:
            assert dataset.id
            assert (await async_knowledge.datasets.retrieve(dataset)).id == dataset.id
        finally:
            await async_knowledge.datasets.delete(dataset)

    async def test_a_document_is_added_and_waited_for(self, async_knowledge, dataset):
        docs = async_knowledge.documents(dataset)
        document = await docs.create(
            text="The refund window is 30 days.", name="policy"
        )

        status = await docs.wait_until_indexed(document, timeout=90)
        assert status.indexed

        hits = await async_knowledge.datasets.search(dataset, "how long to return?")
        assert hits

    async def test_a_listing_walks_its_own_pages(self, async_knowledge, dataset):
        page = await async_knowledge.datasets.list(limit=1)

        assert isinstance(page, AsyncPage)
        assert dataset.id in {found.id async for found in page.all()}

    async def test_the_workspaces_models_come_back(self, async_knowledge):
        """A dataset-token route, so it answers here and 401s on an app key."""
        assert isinstance(await async_knowledge.models(), list)

    async def test_a_base_without_a_pipeline_says_so(self, async_knowledge, dataset):
        from dify_client.exceptions import APIError

        with pytest.raises(APIError, match="has no RAG pipeline"):
            await async_knowledge.pipeline(dataset).datasources()
