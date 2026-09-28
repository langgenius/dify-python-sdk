"""Conversions that threw away what the caller needed.

Each of these turned a rich server reply into a narrower type and dropped the
difference, with nothing to say it had happened.
"""

import asyncio
import inspect

import httpx
import pytest

from dify_client import AsyncDifyApp, DifyApp, DifyKnowledge
from dify_client.exceptions import ValidationError
from dify_client.resources.knowledge import _created_document
from dify_client.results import HistoryMessage, Page


def app(handler, **kwargs):
    return DifyApp(
        "k",
        user="alice",
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://x/v1"
        ),
        **kwargs,
    )


HISTORY = {
    "limit": 20,
    "has_more": True,
    "data": [
        {
            "id": "m1",
            "conversation_id": "c1",
            "query": "what is the refund window?",
            "answer": "30 days",
            "inputs": {"tone": "short"},
            "message_files": [{"id": "f1", "type": "image"}],
            "feedback": {"rating": "like"},
            "retriever_resources": [{"document_name": "policy"}],
            "message_tokens": 10,
            "answer_tokens": 5,
            "total_tokens": 15,
            "total_price": "0.001",
            "currency": "USD",
            "status": "normal",
            "created_at": 1700000000,
        }
    ],
}


class TestConversationHistory:
    """It reused the reply conversion, so every turn lost the question."""

    def _page(self):
        return app(lambda r: httpx.Response(200, json=HISTORY)).chat.messages.list("c1")

    def test_the_question_survives(self):
        assert self._page()[0].query == "what is the refund window?"

    def test_a_transcript_can_be_reconstructed(self):
        turn = self._page()[0]
        assert (turn.query, turn.answer) == ("what is the refund window?", "30 days")

    def test_history_is_its_own_type(self):
        """A generated reply and a recorded turn are different things: one has
        a task to stop, the other has a question and a rating."""
        turn = self._page()[0]
        assert isinstance(turn, HistoryMessage)
        assert not hasattr(turn, "task_id")

    def test_the_attachments_survive(self):
        assert self._page()[0].files == [{"id": "f1", "type": "image"}]

    def test_the_rating_survives(self):
        assert self._page()[0].feedback == "like"

    def test_what_it_cost_survives(self):
        usage = self._page()[0].usage
        assert usage.total_tokens == 15
        assert usage.prompt_tokens == 10

    def test_everything_else_is_kept_whole(self):
        assert self._page()[0].payload["status"] == "normal"

    def test_paging_is_not_dropped(self):
        """A short page and the last page used to look the same."""
        page = self._page()
        assert isinstance(page, Page)
        assert page.has_more
        assert page.limit == 20

    def test_a_page_reads_like_the_list_it_replaced(self):
        page = self._page()
        assert len(page) == 1
        assert [t.id for t in page] == ["m1"]
        assert bool(page)


class TestDocumentIndexing:
    """update() dropped the batch, so asking about indexing built
    /documents//indexing-status."""

    def test_create_keeps_the_batch(self):
        document = _created_document({"document": {"id": "d1"}, "batch": "b1"})
        assert document.batch == "b1"

    def test_update_keeps_it_too(self):
        """Both go through the same conversion now, so they cannot diverge."""
        document = _created_document({"document": {"id": "d1"}, "batch": "b2"})
        assert document.batch == "b2"

    def test_a_document_with_no_batch_says_so_rather_than_building_a_bad_path(self):
        from dify_client.resources.knowledge import Document

        knowledge = DifyKnowledge("k", base_url="https://x/v1")
        with pytest.raises(ValidationError, match="no indexing batch"):
            knowledge.documents("ds").indexing_status(Document(id="d1"))

    def test_the_reply_shape_without_nesting_is_handled(self):
        assert _created_document({"id": "d1", "batch": "b3"}).batch == "b3"


class TestAsyncIsTheSameSurface:
    """The README said "same shape throughout" while async lacked the stop and
    resume calls — so a stream could be started and never finished."""

    PAIRS = [
        (
            "chat.messages",
            ["create", "stream", "list", "stop", "feedback", "suggested"],
        ),
        ("chat.conversations", ["list", "rename", "delete", "variables"]),
        ("workflows.runs", ["create", "stream", "retrieve", "events", "stop"]),
        ("completions", ["create", "stream", "stop"]),
        ("files", ["upload"]),
        ("forms", ["retrieve", "submit"]),
        ("audio", ["speak", "transcribe"]),
        ("annotations", ["list", "create", "update", "delete", "set_reply"]),
    ]

    def _reach(self, client, path):
        for part in path.split("."):
            client = getattr(client, part)
        return client

    @pytest.mark.parametrize(("path", "verbs"), PAIRS, ids=lambda v: str(v)[:24])
    def test_the_async_app_has_the_same_verbs(self, path, verbs):
        asynchronous = self._reach(AsyncDifyApp("k", base_url="https://x/v1"), path)
        missing = [v for v in verbs if not hasattr(asynchronous, v)]
        assert missing == []

    @pytest.mark.parametrize(("path", "verbs"), PAIRS, ids=lambda v: str(v)[:24])
    def test_and_they_are_coroutines(self, path, verbs):
        asynchronous = self._reach(AsyncDifyApp("k", base_url="https://x/v1"), path)
        for verb in verbs:
            method = getattr(asynchronous, verb)
            assert inspect.iscoroutinefunction(method), f"{path}.{verb}"

    def test_a_started_stream_can_be_stopped(self):
        """The gap that mattered: no stop meant no way to finish."""
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            return httpx.Response(200, json={})

        async def call():
            app = AsyncDifyApp(
                "k",
                user="alice",
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), base_url="https://x/v1"
                ),
            )
            await app.chat.messages.stop("task-1")

        asyncio.run(call())
        assert seen["path"].endswith("/chat-messages/task-1/stop")


class TestNodeExecutionsAreIdentifiable:
    def test_an_execution_carries_its_own_id(self):
        from dify_client.results import NodeExecution

        execution = NodeExecution(
            node_id="llm", node_type="llm", status="succeeded", execution_id="e1"
        )
        assert execution.execution_id == "e1"
        assert execution.node_id != execution.execution_id

    def test_a_redelivered_execution_is_not_counted_twice(self):
        """Reconnecting resends events; counting them again doubles the bill."""
        import json

        from dify_client.streams import WorkflowRunStream

        event = {
            "event": "node_finished",
            "data": {
                "id": "e1",
                "node_id": "llm",
                "status": "succeeded",
                "execution_metadata": {"total_tokens": 100},
            },
        }
        response = httpx.Response(
            200, text="".join(f"data: {json.dumps(event)}\n\n" for _ in range(2))
        )
        with WorkflowRunStream(response) as stream:
            list(stream)
            run = stream.get_final_run()

        assert run.usage.total_tokens == 100

    def test_repeats_of_the_same_node_still_count_separately(self):
        import json

        from dify_client.streams import WorkflowRunStream

        def pass_(execution_id):
            return {
                "event": "node_finished",
                "data": {
                    "id": execution_id,
                    "node_id": "llm",
                    "status": "succeeded",
                    "execution_metadata": {"total_tokens": 100},
                },
            }

        response = httpx.Response(
            200,
            text="".join(f"data: {json.dumps(pass_(i))}\n\n" for i in ("e1", "e2")),
        )
        with WorkflowRunStream(response) as stream:
            list(stream)
            run = stream.get_final_run()

        assert run.usage.total_tokens == 200


class TestResourcesKnowWhatTheyPlugInto:
    """They took `Any`, so wiring one to the wrong client was a runtime 404."""

    def test_the_two_contracts_are_distinct(self):
        from dify_client.resources._base import Console, Transport

        assert Transport is not Console

    def test_a_service_api_client_satisfies_transport(self):
        from dify_client.resources._base import Transport

        assert isinstance(DifyApp("k", base_url="https://x/v1"), Transport)

    def test_a_console_client_does_not(self):
        from dify_client import DifyManagement
        from dify_client.resources._base import Transport

        assert not isinstance(
            DifyManagement(token="ey.x", base_url="https://x"), Transport
        )

    def test_and_the_reverse(self):
        from dify_client import DifyManagement
        from dify_client.resources._base import Console

        assert isinstance(DifyManagement(token="ey.x", base_url="https://x"), Console)
        assert not isinstance(DifyApp("k", base_url="https://x/v1"), Console)
