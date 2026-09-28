"""The resource-shaped API: a client that holds the connection, resources that
hold the verbs, and return values you can hand back to Dify.

What it replaces: operations piled onto ChatClient and WorkflowClient, and
`response.json()["data"]["outputs"]` at every call site.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from dify_client import (
    AsyncDifyApp,
    Conversation,
    DifyApp,
    Message,
    UploadedFile,
    WorkflowRun,
)
from dify_client.exceptions import ValidationError

BLOCKING_RUN = {
    "task_id": "task-1",
    "workflow_run_id": "run-1",
    "data": {
        "id": "run-1",
        "status": "succeeded",
        "outputs": {"answer": "done"},
        "total_tokens": 50,
        "elapsed_time": 1.5,
    },
}

BLOCKING_MESSAGE = {
    "event": "message",
    "task_id": "task-2",
    "id": "m-2",
    "message_id": "m-2",
    "conversation_id": "conv-2",
    "answer": "Hello there",
    "metadata": {
        "usage": {"total_tokens": 20, "total_price": "0.001", "currency": "USD"}
    },
    "created_at": 1700000000,
}

STREAMED_RUN = [
    {"event": "workflow_started", "task_id": "task-3", "data": {"id": "run-3"}},
    {
        "event": "node_finished",
        "task_id": "task-3",
        "data": {
            "node_id": "llm",
            "node_type": "llm",
            "status": "succeeded",
            "outputs": {"text": "hi"},
            "execution_metadata": {
                "total_tokens": 30,
                "total_price": "0.002",
                "currency": "USD",
            },
        },
    },
    {
        "event": "workflow_finished",
        "task_id": "task-3",
        "data": {"status": "succeeded", "outputs": {"answer": "hi"}},
    },
]


class Dify:
    """Answers the Service API, and remembers what it was asked."""

    def __init__(self, **routes):
        self.routes = routes
        self.calls = []

    def handler(self, request):
        path = request.url.path.replace("/v1", "", 1)
        body = None
        if request.content and request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            body = json.loads(request.content)
        self.calls.append(
            {
                "method": request.method,
                "path": path,
                "body": body,
                "params": dict(request.url.params),
                "content": request.content,
            }
        )
        for route, reply in self.routes.items():
            if path == route:
                return reply() if callable(reply) else reply
        return httpx.Response(404, json={"message": f"no stub for {path}"})

    def app(self, **kwargs) -> DifyApp:
        return DifyApp(
            "app-key",
            http_client=httpx.Client(
                transport=httpx.MockTransport(self.handler),
                base_url="https://dify.test/v1",
            ),
            **kwargs,
        )


def sse(events):
    return httpx.Response(
        200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events)
    )


class TestShape:
    def test_the_verbs_live_under_what_they_act_on(self):
        app = Dify().app(user="alice")
        assert callable(app.workflows.runs.create)
        assert callable(app.chat.messages.create)
        assert callable(app.chat.conversations.list)
        assert callable(app.files.upload)

    def test_the_app_is_the_connection_not_a_grab_bag(self):
        """Operations hang off resources; the client holds credential and pool."""
        app = Dify().app(user="alice")
        assert not hasattr(app, "create_chat_message")
        assert app.api_key == "app-key"

    def test_the_key_is_not_rendered(self):
        assert "app-key" not in repr(Dify().app(user="alice"))


class TestWorkflowRuns:
    def test_create_returns_a_run_not_a_response(self):
        dify = Dify(**{"/workflows/run": httpx.Response(200, json=BLOCKING_RUN)})
        run = dify.app(user="alice").workflows.runs.create({"text": "x"})

        assert isinstance(run, WorkflowRun)
        assert run.outputs == {"answer": "done"}
        assert run.status == "succeeded"

    def test_it_carries_the_ids_the_next_call_needs(self):
        """run_id reads it back; task_id stops it. They are different things."""
        dify = Dify(**{"/workflows/run": httpx.Response(200, json=BLOCKING_RUN)})
        run = dify.app(user="alice").workflows.runs.create({"text": "x"})

        assert run.run_id == "run-1"
        assert run.task_id == "task-1"

    def test_blocking_usage_is_not_reported_as_zero(self):
        """Dify gives one token count in blocking mode and no per-node detail."""
        dify = Dify(**{"/workflows/run": httpx.Response(200, json=BLOCKING_RUN)})
        run = dify.app(user="alice").workflows.runs.create({"text": "x"})

        assert run.usage.total_tokens == 50

    def test_create_asks_for_blocking(self):
        dify = Dify(**{"/workflows/run": httpx.Response(200, json=BLOCKING_RUN)})
        dify.app(user="alice").workflows.runs.create({"text": "x"})

        assert dify.calls[0]["body"]["response_mode"] == "blocking"
        assert dify.calls[0]["body"]["user"] == "alice"

    def test_retrieve_reads_one_back(self):
        dify = Dify(**{"/workflows/run/run-1": httpx.Response(200, json=BLOCKING_RUN)})
        run = dify.app(user="alice").workflows.runs.retrieve("run-1")

        assert run.run_id == "run-1"

    def test_stop_names_the_task_not_the_run(self):
        dify = Dify(
            **{
                "/workflows/run": httpx.Response(200, json=BLOCKING_RUN),
                "/workflows/tasks/task-1/stop": httpx.Response(200, json={}),
            }
        )
        app = dify.app(user="alice")
        run = app.workflows.runs.create({"text": "x"})
        app.workflows.runs.stop(run)

        assert dify.calls[-1]["path"] == "/workflows/tasks/task-1/stop"

    def test_stopping_a_run_with_no_task_id_says_why_not(self):
        app = Dify().app(user="alice")
        with pytest.raises(ValidationError, match="nothing to stop"):
            app.workflows.runs.stop(WorkflowRun(status="succeeded"))


class TestStreamingARun:
    def _app(self):
        return Dify(**{"/workflows/run": lambda: sse(STREAMED_RUN)}).app(user="alice")

    def test_events_arrive_typed(self):
        with self._app().workflows.runs.stream({"x": 1}) as stream:
            finished = [e for e in stream if e.type == "node_finished"]

        assert finished[0].execution.node_id == "llm"
        assert finished[0].execution.usage.total_tokens == 30

    def test_the_final_run_is_the_same_type_create_returns(self):
        with self._app().workflows.runs.stream({"x": 1}) as stream:
            list(stream)
            run = stream.get_final_run()

        assert isinstance(run, WorkflowRun)
        assert run.outputs == {"answer": "hi"}
        assert run.run_id == "run-3"

    def test_the_stream_asks_httpx_not_to_buffer(self):
        dify = Dify(**{"/workflows/run": lambda: sse(STREAMED_RUN)})
        with dify.app(user="alice").workflows.runs.stream({"x": 1}) as stream:
            list(stream)
        assert dify.calls[0]["body"]["response_mode"] == "streaming"

    def test_the_unknown_parts_of_an_event_are_kept(self):
        """A newer Dify must not lose information passing through."""
        future = [
            {
                "event": "node_finished",
                "data": {"node_id": "n"},
                "something_new": {"deep": 1},
            }
        ]
        dify = Dify(**{"/workflows/run": lambda: sse(future)})
        with dify.app(user="alice").workflows.runs.stream({"x": 1}) as stream:
            (event,) = list(stream)
        assert event["something_new"] == {"deep": 1}

    def test_closing_the_stream_releases_the_connection(self):
        dify = Dify(**{"/workflows/run": lambda: sse(STREAMED_RUN)})
        stream = dify.app(user="alice").workflows.runs.stream({"x": 1})
        next(iter(stream))
        stream.close()
        assert stream._response.is_closed

    def test_a_paused_run_is_neither_succeeded_nor_failed(self):
        """Waiting on a person is its own state; the review called this out."""
        paused = [
            {"event": "workflow_started", "task_id": "t", "data": {"id": "r"}},
            {"event": "human_input_required", "data": {"form_token": "tok"}},
        ]
        dify = Dify(**{"/workflows/run": lambda: sse(paused)})
        with dify.app(user="alice").workflows.runs.stream({"x": 1}) as stream:
            list(stream)
            run = stream.get_final_run()

        assert run.paused
        assert not run.succeeded
        assert not run.failed
        assert run.pending_forms == ["tok"]

    def test_reopening_a_stream_uses_the_events_route(self):
        dify = Dify(**{"/workflow/run-3/events": lambda: sse(STREAMED_RUN)})
        with dify.app(user="alice").workflows.runs.events("run-3") as stream:
            list(stream)
        assert dify.calls[0]["params"]["continue_on_pause"] == "false"

    def test_resuming_past_a_pause_is_asked_for_explicitly(self):
        dify = Dify(**{"/workflow/r/events": lambda: sse(STREAMED_RUN)})
        with dify.app(user="alice").workflows.runs.events("r", resume_paused=True) as s:
            list(s)
        assert dify.calls[0]["params"]["continue_on_pause"] == "true"


class TestMessages:
    def test_create_returns_a_message(self):
        dify = Dify(**{"/chat-messages": httpx.Response(200, json=BLOCKING_MESSAGE)})
        message = dify.app(user="alice").chat.messages.create("Hello")

        assert isinstance(message, Message)
        assert message.answer == "Hello there"
        assert str(message) == "Hello there"

    def test_it_carries_the_thread(self):
        dify = Dify(**{"/chat-messages": httpx.Response(200, json=BLOCKING_MESSAGE)})
        message = dify.app(user="alice").chat.messages.create("Hello")

        assert message.conversation_id == "conv-2"
        assert message.message_id == "m-2"
        assert message.task_id == "task-2"

    def test_usage_comes_out_of_difys_metadata(self):
        dify = Dify(**{"/chat-messages": httpx.Response(200, json=BLOCKING_MESSAGE)})
        message = dify.app(user="alice").chat.messages.create("Hello")

        assert message.usage.total_tokens == 20

    def test_the_metadata_is_kept_whole(self):
        """Picking it apart would drop whatever a newer Dify adds."""
        dify = Dify(**{"/chat-messages": httpx.Response(200, json=BLOCKING_MESSAGE)})
        message = dify.app(user="alice").chat.messages.create("Hello")

        assert "usage" in message.metadata

    def test_continuing_a_thread_passes_its_id(self):
        dify = Dify(**{"/chat-messages": httpx.Response(200, json=BLOCKING_MESSAGE)})
        app = dify.app(user="alice")
        first = app.chat.messages.create("Hello")
        app.chat.messages.create("And again", conversation_id=first.conversation_id)

        assert dify.calls[-1]["body"]["conversation_id"] == "conv-2"

    def test_a_new_thread_sends_no_conversation_id(self):
        dify = Dify(**{"/chat-messages": httpx.Response(200, json=BLOCKING_MESSAGE)})
        dify.app(user="alice").chat.messages.create("Hello")

        assert "conversation_id" not in dify.calls[0]["body"]

    def test_feedback_takes_the_message_itself(self):
        dify = Dify(
            **{
                "/chat-messages": httpx.Response(200, json=BLOCKING_MESSAGE),
                "/messages/m-2/feedbacks": httpx.Response(200, json={}),
            }
        )
        app = dify.app(user="alice")
        message = app.chat.messages.create("Hello")
        app.chat.messages.feedback(message, "like", content="useful")

        assert dify.calls[-1]["body"] == {
            "rating": "like",
            "user": "alice",
            "content": "useful",
        }

    def test_a_rating_can_be_revoked(self):
        dify = Dify(**{"/messages/m-9/feedbacks": httpx.Response(200, json={})})
        dify.app(user="alice").chat.messages.feedback("m-9", None)

        assert dify.calls[-1]["body"]["rating"] is None


class TestStreamingAMessage:
    STREAM = [
        {
            "event": "message",
            "task_id": "t",
            "message_id": "m",
            "conversation_id": "c",
            "answer": "Hel",
        },
        {
            "event": "message",
            "task_id": "t",
            "message_id": "m",
            "conversation_id": "c",
            "answer": "lo",
        },
        {
            "event": "message_end",
            "task_id": "t",
            "message_id": "m",
            "conversation_id": "c",
            "metadata": {"usage": {"total_tokens": 7}},
        },
    ]

    def _app(self):
        return Dify(**{"/chat-messages": lambda: sse(self.STREAM)}).app(user="alice")

    def test_the_text_arrives_in_pieces(self):
        with self._app().chat.messages.stream("Hello") as stream:
            assert list(stream.text()) == ["Hel", "lo"]

    def test_the_final_message_is_assembled(self):
        with self._app().chat.messages.stream("Hello") as stream:
            list(stream)
            message = stream.get_final_message()

        assert message.answer == "Hello"
        assert message.conversation_id == "c"
        assert message.usage.total_tokens == 7

    def test_it_can_be_read_mid_stream(self):
        """The point of watching: ask what it has come to so far."""
        with self._app().chat.messages.stream("Hello") as stream:
            events = iter(stream)
            next(events)
            assert stream.get_final_message().answer == "Hel"


class TestConversations:
    LIST = {
        "data": [
            {"id": "c1", "name": "Outage", "status": "normal", "created_at": 1},
            {"id": "c2", "name": "Billing", "status": "normal"},
        ]
    }

    def test_they_come_back_typed(self):
        dify = Dify(**{"/conversations": httpx.Response(200, json=self.LIST)})
        threads = dify.app(user="alice").chat.conversations.list()

        assert threads[0] == Conversation(
            id="c1", name="Outage", status="normal", created_at=1
        )

    def test_listing_pages_by_cursor(self):
        dify = Dify(**{"/conversations": httpx.Response(200, json=self.LIST)})
        dify.app(user="alice").chat.conversations.list(last_id="c1", limit=5)

        assert dify.calls[0]["params"]["last_id"] == "c1"
        assert "page" not in dify.calls[0]["params"]

    def test_deleting_takes_the_conversation_itself(self):
        dify = Dify(
            **{
                "/conversations": httpx.Response(200, json=self.LIST),
                "/conversations/c1": httpx.Response(204),
            }
        )
        app = dify.app(user="alice")
        thread = app.chat.conversations.list()[0]
        app.chat.conversations.delete(thread)

        assert dify.calls[-1]["path"] == "/conversations/c1"


class TestFiles:
    REPLY = httpx.Response(
        201,
        json={
            "id": "file-1",
            "name": "report.pdf",
            "size": 2,
            "mime_type": "application/pdf",
        },
    )

    def test_uploading_returns_the_reference_not_a_response(self, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"hi")
        dify = Dify(**{"/files/upload": self.REPLY})

        uploaded = dify.app(user="alice").files.upload(pdf)

        assert isinstance(uploaded, UploadedFile)
        assert uploaded.id == "file-1"

    def test_the_reference_is_what_an_input_carries(self, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"hi")
        dify = Dify(**{"/files/upload": self.REPLY})

        uploaded = dify.app(user="alice").files.upload(pdf)

        assert uploaded.reference() == {
            "transfer_method": "local_file",
            "upload_file_id": "file-1",
            "type": "document",
        }

    def test_an_unnamed_stream_asks_for_a_filename(self):
        import io

        with pytest.raises(ValidationError, match="filename"):
            Dify().app(user="alice").files.upload(io.BytesIO(b"x"))


class TestTheUserIdentifier:
    def test_it_can_be_set_once_on_the_client(self):
        dify = Dify(**{"/workflows/run": httpx.Response(200, json=BLOCKING_RUN)})
        dify.app(user="alice").workflows.runs.create({"x": 1})

        assert dify.calls[0]["body"]["user"] == "alice"

    def test_a_call_can_override_it(self):
        dify = Dify(**{"/workflows/run": httpx.Response(200, json=BLOCKING_RUN)})
        dify.app(user="alice").workflows.runs.create({"x": 1}, user="bob")

        assert dify.calls[0]["body"]["user"] == "bob"

    def test_with_neither_it_says_what_is_missing(self):
        app = Dify().app()
        with pytest.raises(ValidationError, match="end-user identifier"):
            app.workflows.runs.create({"x": 1})


class TestAppInfo:
    INFO = httpx.Response(
        200,
        json={
            "name": "triage",
            "mode": "advanced-chat",
            "description": "d",
            "tags": ["a"],
        },
    )

    def test_the_mode_is_reported(self):
        dify = Dify(**{"/info": self.INFO})
        info = dify.app(user="alice").info()

        assert info.mode == "advanced-chat"
        assert info.is_chat
        assert not info.is_workflow

    def test_open_checks_before_handing_the_client_back(self):
        dify = Dify(**{"/info": self.INFO})
        DifyApp.open(
            "app-key",
            http_client=httpx.Client(
                transport=httpx.MockTransport(dify.handler),
                base_url="https://dify.test/v1",
            ),
        )
        assert dify.calls[0]["path"] == "/info"


class TestAsync:
    def _app(self, dify, **kwargs):
        return AsyncDifyApp(
            "app-key",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(dify.handler),
                base_url="https://dify.test/v1",
            ),
            **kwargs,
        )

    def test_a_message_comes_back_the_same_type(self):
        dify = Dify(**{"/chat-messages": httpx.Response(200, json=BLOCKING_MESSAGE)})

        async def call():
            async with self._app(dify, user="alice") as app:
                return await app.chat.messages.create("Hello")

        assert asyncio.run(call()).answer == "Hello there"

    def test_a_run_comes_back_the_same_type(self):
        dify = Dify(**{"/workflows/run": httpx.Response(200, json=BLOCKING_RUN)})

        async def call():
            async with self._app(dify, user="alice") as app:
                return await app.workflows.runs.create({"x": 1})

        assert asyncio.run(call()).outputs == {"answer": "done"}

    def test_streaming_assembles_the_same_way(self):
        dify = Dify(**{"/chat-messages": lambda: sse(TestStreamingAMessage.STREAM)})

        async def call():
            async with self._app(dify, user="alice") as app:
                stream = await app.chat.messages.stream("Hello")
                pieces = [p async for p in stream.text()]
                return pieces, stream.get_final_message()

        pieces, message = asyncio.run(call())
        assert pieces == ["Hel", "lo"]
        assert message.answer == "Hello"


class TestTypesDoNotNeedGraphon:
    """The Service-API surface must work without the workflow extra."""

    #: Everything outside `dify_client/workflow/`. Listing six modules by hand
    #: was how `openapi.py` came to import the workflow package and drag
    #: graphon into a plain install — the module simply was not on the list.
    @staticmethod
    def _service_api_modules():
        root = Path(__import__("dify_client").__file__).parent
        return sorted(
            path
            for path in root.rglob("*.py")
            if "workflow" not in path.relative_to(root).parts
            and path.name not in {"agent.py", "skills.py"}
        )

    def test_there_are_modules_to_check(self):
        assert len(self._service_api_modules()) > 10

    def test_no_module_on_this_path_imports_graphon(self):
        """Checked on the imports, not the prose — the docstrings say why.

        Every module, including transitively: importing
        `dify_client.workflow.anything` runs that package's `__init__`, which
        imports the builder, which needs graphon.
        """
        import ast

        def imports(tree):
            """Names imported at run time. `if TYPE_CHECKING` blocks are not:
            they exist for the type checker and never execute."""
            for node in ast.walk(tree):
                if isinstance(node, ast.If):
                    guard = ast.unparse(node.test)
                    if "TYPE_CHECKING" in guard:
                        continue
                if isinstance(node, ast.Import):
                    yield from (alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    yield node.module

        def live(tree):
            skipped = {
                id(child)
                for node in ast.walk(tree)
                if isinstance(node, ast.If)
                and "TYPE_CHECKING" in ast.unparse(node.test)
                for child in ast.walk(node)
            }
            for node in ast.walk(tree):
                if id(node) in skipped:
                    continue
                if isinstance(node, ast.Import):
                    yield from (alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    yield node.module

        offenders = []
        for path in self._service_api_modules():
            for name in live(ast.parse(path.read_text())):
                if name.startswith("graphon") or "workflow" in name.split("."):
                    offenders.append(f"{path.name}: {name}")
        assert offenders == []

    def test_the_result_types_live_outside_the_workflow_package(self):
        import dify_client.results as results

        assert results.__name__ == "dify_client.results"
