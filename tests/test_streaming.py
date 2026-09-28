"""Reading a streaming response: the SSE layer, and what it feeds.

There used to be two ways to do this — `stream_events(response)` yielding one
event type, and `WorkflowRunStream` yielding another — for the same bytes. The
standalone helpers took a raw `httpx.Response`, which no public method hands
out any more, so they were unreachable as well as duplicated. One way now:
decode a line (`dify_client.sse`), and watch a run (`dify_client.streams`).
"""

import asyncio
import json

import httpx
import pytest

from dify_client import MessageStream, RunEvent, WorkflowRunStream
from dify_client.exceptions import APIError
from dify_client.sse import TEXT_FIELDS, decode, text_of
from dify_client.streams import AsyncMessageStream, AsyncWorkflowRunStream


def sse(*objects) -> httpx.Response:
    return httpx.Response(
        200, text="".join(f"data: {json.dumps(o)}\n\n" for o in objects)
    )


CHAT = [
    {"event": "message", "task_id": "t", "answer": "Hel"},
    {"event": "message", "task_id": "t", "answer": "lo"},
    {"event": "message_end", "task_id": "t", "metadata": {}},
]

WORKFLOW = [
    {"event": "workflow_started", "data": {"id": "r1"}},
    {"event": "text_chunk", "data": {"text": "- ship"}},
    {"event": "text_chunk", "data": {"text": " by friday"}},
    {
        "event": "workflow_finished",
        "data": {"status": "succeeded", "outputs": {"a": 1}},
    },
]


class TestDecodingOneLine:
    def test_a_data_line_becomes_its_payload(self):
        assert decode('data: {"event": "message"}') == {"event": "message"}

    @pytest.mark.parametrize(
        "line",
        ["", "   ", ": a comment", "data:", "data:   ", "event: message"],
        ids=lambda v: repr(v),
    )
    def test_nothing_useful_decodes_to_nothing(self, line):
        assert decode(line) is None

    def test_a_malformed_line_ends_that_line_not_the_stream(self):
        assert decode("data: {not json") is None

    def test_a_json_scalar_is_not_an_event(self):
        assert decode('data: "just a string"') is None

    def test_keepalives_are_dropped(self):
        assert decode('data: {"event": "ping"}') is None


class TestWhereTheTextIs:
    """A chatflow puts it at the top level; a workflow app one level down.
    Getting it wrong yields silence, not an error."""

    def test_a_chatflow_message(self):
        assert text_of({"event": "message", "answer": "hi"}) == "hi"

    def test_a_workflow_text_chunk(self):
        assert text_of({"event": "text_chunk", "data": {"text": "hi"}}) == "hi"

    def test_an_agent_message_counts_too(self):
        assert text_of({"event": "agent_message", "answer": "thinking"}) == "thinking"

    def test_an_event_that_carries_none(self):
        assert text_of({"event": "node_finished", "data": {"node_id": "n"}}) == ""

    def test_the_mapping_is_written_down_once(self):
        assert set(TEXT_FIELDS) == {"message", "agent_message", "text_chunk"}


class TestWatchingARun:
    def test_the_events_arrive_in_order(self):
        with WorkflowRunStream(sse(*WORKFLOW)) as stream:
            assert [e.type for e in stream] == [
                "workflow_started",
                "text_chunk",
                "text_chunk",
                "workflow_finished",
            ]

    def test_keepalives_and_junk_do_not_interrupt(self):
        noisy = sse({"event": "ping"}, *WORKFLOW, {"event": "ping"})
        with WorkflowRunStream(noisy) as stream:
            assert len(list(stream)) == 4

    def test_the_text_is_assembled(self):
        with WorkflowRunStream(sse(*WORKFLOW)) as stream:
            list(stream)
            assert "".join(stream.get_final_run().stream) == "- ship by friday"

    def test_an_event_keeps_its_whole_payload(self):
        """A field a newer Dify adds must still reach the caller."""
        with WorkflowRunStream(
            sse({"event": "node_finished", "surprise": 1})
        ) as stream:
            (event,) = list(stream)
        assert isinstance(event, RunEvent)
        assert event["surprise"] == 1

    def test_the_nested_data_is_reachable_without_digging(self):
        with WorkflowRunStream(sse(*WORKFLOW)) as stream:
            events = list(stream)
        assert events[-1].data["status"] == "succeeded"


class TestReadingAMessage:
    def test_the_answer_arrives_in_pieces(self):
        with MessageStream(sse(*CHAT)) as stream:
            assert list(stream.text()) == ["Hel", "lo"]

    def test_and_is_assembled_at_the_end(self):
        with MessageStream(sse(*CHAT)) as stream:
            list(stream)
            assert stream.get_final_message().answer == "Hello"


class TestErrors:
    """Dify reports a mid-stream failure as an event, after sending a 200."""

    ERROR = [
        {"event": "message", "answer": "par"},
        {"event": "error", "status": 400, "code": "bad", "message": "model refused"},
    ]

    def test_an_error_event_is_raised_not_swallowed(self):
        with pytest.raises(APIError, match="model refused"):
            with MessageStream(sse(*self.ERROR)) as stream:
                list(stream)

    def test_the_text_before_it_still_arrived(self):
        pieces = []
        with pytest.raises(APIError):
            with MessageStream(sse(*self.ERROR)) as stream:
                for piece in stream.text():
                    pieces.append(piece)
        assert pieces == ["par"]

    def test_the_status_travels_with_it(self):
        with pytest.raises(APIError) as caught:
            with WorkflowRunStream(sse(*self.ERROR)) as stream:
                list(stream)
        assert caught.value.status_code == 400

    def test_it_can_be_left_as_an_event_instead(self):
        with WorkflowRunStream(sse(*self.ERROR), raise_on_error=False) as stream:
            events = list(stream)
        assert events[-1].type == "error"


class TestClosing:
    def test_the_response_is_closed_when_the_stream_ends(self):
        reply = sse(*CHAT)
        with MessageStream(reply) as stream:
            list(stream)
        assert reply.is_closed

    def test_it_is_closed_when_the_loop_is_abandoned(self):
        """Otherwise an early break leaks the connection."""
        reply = sse(*CHAT)
        stream = MessageStream(reply)
        next(iter(stream))
        stream.close()
        assert reply.is_closed

    def test_closing_twice_is_harmless(self):
        stream = MessageStream(sse(*CHAT))
        stream.close()
        stream.close()


class TestAsync:
    def test_text_arrives_the_same_way(self):
        async def read():
            stream = AsyncMessageStream(sse(*CHAT))
            return [p async for p in stream.text()]

        assert asyncio.run(read()) == ["Hel", "lo"]

    def test_events_arrive_the_same_way(self):
        async def read():
            stream = AsyncWorkflowRunStream(sse(*WORKFLOW))
            return [e.type async for e in stream]

        assert asyncio.run(read())[0] == "workflow_started"

    def test_errors_raise_the_same_way(self):
        async def read():
            stream = AsyncMessageStream(sse(*TestErrors.ERROR))
            return [p async for p in stream.text()]

        with pytest.raises(APIError, match="model refused"):
            asyncio.run(read())


class TestThereIsOnlyOneWay:
    """The duplicates are gone, and the decoder is not public."""

    @pytest.mark.parametrize(
        "name",
        [
            "StreamEvent",
            "stream_text",
            "stream_events",
            "astream_text",
            "astream_events",
        ],
    )
    def test_the_old_helper_is_gone(self, name):
        import dify_client

        assert not hasattr(dify_client, name)

    def test_the_old_module_is_gone(self):
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("dify_client.streaming")

    def test_the_decoder_is_internal(self):
        """It answers "what is on this line", not "what happened to my run"."""
        import dify_client

        assert "decode" not in dify_client.__all__
        assert "sse" not in dify_client.__all__

    def test_one_event_type_reaches_callers(self):
        import dify_client

        assert "RunEvent" in dify_client.__all__
