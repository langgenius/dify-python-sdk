"""Watching a run happen, and asking what it came to.

Three things are separate here, and Dify makes the difference matter:

* **Closing the stream** stops watching. It does not stop the run — that is
  ``runs.stop()``, which Dify has its own endpoint for.
* **A pause** is the run waiting for a person, not failing. The stream ends and
  the run is neither succeeded nor failed.
* **A dropped connection** says nothing about the run at all. Reopen it with
  ``runs.stream_events(run_id)`` and carry on.

So a stream yields typed events while it lasts, and then answers what the run
came to — which may be "still waiting".
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from .results import Message, NodeExecution, WorkflowRun
from .sse import decode, text_of
from .usage import Usage

__all__ = ["MessageStream", "RunEvent", "WorkflowRunStream", "collect_run"]


def collect_run(lines: Iterable[str]) -> WorkflowRun:
    """Read a whole Dify event stream into one :class:`WorkflowRun`.

    The same accumulation the streams do, for callers that have the lines and
    no interest in watching them go by. There was a second copy of this in the
    workflow package, and fixes landed in one of them: a streamed run kept its
    price here and lost it there.

    Takes lines rather than a response so nothing about transports leaks in,
    and so :mod:`dify_client.openapi` can use it without reaching into the
    workflow package — which would drag `graphon` into a plain install.
    """
    watcher = _Watcher()
    for line in lines:
        payload = decode(line)
        if payload is not None:
            watcher.see(_to_run_event(payload))
    return watcher.to_run()


#: Dify's own names. A filled form means the run is moving again; a timeout
#: means it is not, and never will be on that form.
_FORM_RAISED = frozenset({"human_input_required"})
_FORM_ANSWERED = frozenset({"human_input_form_filled"})
_FORM_EXPIRED = frozenset({"human_input_form_timeout"})


@dataclass(frozen=True)
class RunEvent:
    """One event, with the parts this SDK understands pulled out.

    ``payload`` is always the whole thing Dify sent, so a field this SDK has
    never heard of still reaches the caller — a newer Dify does not lose
    information on the way through.
    """

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    #: Set on ``node_finished``: which node ran, and what it produced.
    execution: NodeExecution | None = None
    #: Set on ``human_input_required``: the token to answer with. Dify leaves
    #: it out for a form it means to be answered in its own UI, so a pause is
    #: not the same thing as a token.
    form_token: str = ""
    #: The node this event is about, where Dify names one. Human-input events
    #: identify their form by node rather than by token — the filled and
    #: timeout events carry no token at all.
    node_id: str = ""
    #: Set on ``message`` and ``text_chunk``: the piece of the answer.
    text: str = ""

    @property
    def data(self) -> dict[str, Any]:
        nested = self.payload.get("data")
        return nested if isinstance(nested, dict) else {}

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


def _to_run_event(payload: dict[str, Any]) -> RunEvent:
    """One decoded payload as an event about a run."""
    nested = payload.get("data")
    data = nested if isinstance(nested, dict) else {}
    name = str(payload.get("event", ""))
    execution = None
    if name == "node_finished":
        execution = NodeExecution(
            node_id=str(data.get("node_id") or ""),
            node_type=str(data.get("node_type") or ""),
            status=str(data.get("status") or ""),
            execution_id=str(data.get("id") or ""),
            index=int(data.get("index") or 0),
            title=str(data.get("title") or ""),
            inputs=dict(data.get("inputs") or {}),
            outputs=dict(data.get("outputs") or {}),
            process_data=dict(data.get("process_data") or {}),
            error=str(data.get("error") or ""),
            usage=Usage.from_metadata(
                data.get("execution_metadata"), data.get("elapsed_time")
            ),
        )
    return RunEvent(
        type=name,
        payload=payload,
        execution=execution,
        form_token=str(data.get("form_token") or payload.get("form_token") or ""),
        node_id=str(data.get("node_id") or payload.get("node_id") or ""),
        text=text_of(payload),
    )


@dataclass(frozen=True)
class _Waiting:
    """One form a run is stopped at, as the events describe it."""

    node_id: str = ""
    token: str = ""


def _form_key(event: RunEvent) -> str:
    """What identifies the form this event is about.

    The node, where Dify names one — it names a node on every human-input
    event and a token on only some of them.
    """
    return event.node_id or str(event.data.get("form_id") or "")


class _Watcher:
    """Accumulates what the events say, so the stream can be asked at the end."""

    def __init__(self) -> None:
        # Not "running": a stream that ends without a finish event leaves the
        # outcome unlearned, and calling that "running" invites a caller to
        # wait for something that is not coming. The two readers of a Dify
        # stream disagreed on this word until they were made one.
        self.status = "unknown"
        self.outputs: dict[str, Any] = {}
        self.error = ""
        self.nodes: dict[str, NodeExecution] = {}
        self.executions: list[NodeExecution] = []
        self.text: list[str] = []
        # The forms the run is waiting on: what identifies each, against the
        # node and token it belongs to. Keyed that way because that is how
        # Dify settles them — the filled and timeout events name a node and
        # carry no token, so a flat list of tokens could only be cleared
        # wholesale, and answering one of two parallel forms dropped the
        # other. Node and token are kept apart rather than collapsed into the
        # key: Dify sends a form with no node, and a key that stood in for one
        # put a form token in `paused_nodes`.
        self.waiting: dict[str, _Waiting] = {}
        self.run_id = self.task_id = ""
        self.conversation_id = self.message_id = ""
        self.metadata: dict[str, Any] = {}
        self.created_at: int | None = None
        self.finished = False
        #: Why Dify replaced the answer, when it did. Empty otherwise.
        self.replaced_reason = ""
        self.reported_usage: Usage | None = None
        self._seen_executions: set[str] = set()

    @property
    def pending_forms(self) -> list[str]:
        """The tokens still to be answered. A pause Dify gave no token for is
        still a pause — ``waiting`` has it, this does not."""
        return [form.token for form in self.waiting.values() if form.token]

    @property
    def paused_nodes(self) -> list[str]:
        """The nodes the run is waiting at, where Dify named one."""
        return [form.node_id for form in self.waiting.values() if form.node_id]

    def see(self, event: RunEvent) -> None:
        payload, data = event.payload, event.data
        self.task_id = self.task_id or str(payload.get("task_id") or "")
        self.conversation_id = self.conversation_id or str(
            payload.get("conversation_id") or ""
        )
        self.message_id = self.message_id or str(payload.get("message_id") or "")
        self.run_id = self.run_id or str(
            data.get("workflow_run_id") or payload.get("workflow_run_id") or ""
        )
        if event.type in {"workflow_started"} and not self.run_id:
            self.run_id = str(data.get("id") or "")

        if event.execution is not None:
            self.nodes[event.execution.node_id] = event.execution
            # Reconnecting can redeliver an execution already seen. Counting it
            # twice would double its tokens; the execution id is what tells a
            # repeat of one node from a resend of the same execution.
            seen = event.execution.execution_id
            if not seen or seen not in self._seen_executions:
                self.executions.append(event.execution)
                if seen:
                    self._seen_executions.add(seen)
        if event.type == "message_replace":
            # Output moderation rejected the answer and Dify sent a whole
            # replacement — which is also what it saved. It replaces what was
            # streamed rather than adding to it; appending would hand back the
            # rejected text with the replacement stuck on the end.
            self.text = [str(payload.get("answer") or data.get("answer") or "")]
            self.replaced_reason = str(
                payload.get("reason") or data.get("reason") or ""
            )
        elif event.text:
            self.text.append(event.text)
        if event.type in _FORM_ANSWERED | _FORM_EXPIRED:
            # Checked before the branch below, because a form being settled is
            # the opposite of one being raised.
            self._settle(event)
        elif event.type in _FORM_RAISED or event.form_token:
            # Keyed by node where Dify names one, so the right form is settled
            # later. `form_id` stands in when it does not, and the token last
            # — Dify omits the token for a form meant for its own UI.
            self.waiting[_form_key(event) or event.form_token] = _Waiting(
                node_id=event.node_id, token=event.form_token
            )
            # Waiting is not finishing, and it is not failing either.
            self.status = "paused"
        elif event.type == "workflow_paused":
            # Dify says so outright, with the reasons that caused it. Worth
            # handling on its own: a pause with no token would otherwise be
            # invisible, and reopening a stream on a paused run delivers this
            # event without the ones that led to it.
            self.status = "paused"
            for reason in data.get("reasons") or []:
                if isinstance(reason, dict):
                    node = str(reason.get("node_id") or "")
                    token = str(reason.get("form_token") or "")
                    key = node or str(reason.get("form_id") or "") or token
                    if key:
                        self.waiting.setdefault(
                            key, _Waiting(node_id=node, token=token)
                        )
            self.outputs = dict(data.get("outputs") or self.outputs)
            self._see_total(data)
        elif event.type == "workflow_finished":
            self.status = str(data.get("status") or "unknown")
            self.outputs = dict(data.get("outputs") or {})
            self.error = str(data.get("error") or "")
            self.finished = True
            # Dify's own total for the whole run. Reconnecting to a finished
            # run delivers this event and nothing else, so without it the run
            # would report as free.
            self._see_total(data)
        elif event.type == "message_end":
            self.metadata = dict(data.get("metadata") or payload.get("metadata") or {})
            self.finished = True
            if self.status == "unknown":
                self.status = "succeeded"
            self._see_total(self.metadata.get("usage") or {})
        elif event.type == "error":
            self.status = "failed"
            self.error = str(data.get("message") or payload.get("message") or "")
            self.finished = True
        if self.created_at is None and payload.get("created_at") is not None:
            self.created_at = int(payload["created_at"])

    def _settle(self, event: RunEvent) -> None:
        """One form answered, or expired. The rest of the run is unaffected."""
        expired = event.type in _FORM_EXPIRED
        key = _form_key(event) or event.form_token
        if key and key in self.waiting:
            del self.waiting[key]
        elif not key:
            # Nothing to match on. Clearing the lot is the old behaviour, and
            # is only right when there was one form to begin with.
            self.waiting.clear()
        if expired:
            self.error = self.error or "the human-input form expired"
        if self.waiting:
            # Another form is still open, so the run is still waiting.
            return
        if self.status == "paused":
            self.status = "failed" if expired else "unknown"

    def _see_total(self, source: Any) -> None:
        """Keep Dify's figure for the whole run, when it gave one."""
        if not isinstance(source, dict):
            return
        if source.get("total_tokens") or source.get("total_price"):
            self.reported_usage = Usage.from_metadata(
                source, source.get("elapsed_time")
            )

    def to_run(self) -> WorkflowRun:
        return WorkflowRun(
            status=self.status,
            outputs=self.outputs,
            nodes=self.nodes,
            error=self.error,
            stream=self.text,
            executions=self.executions,
            run_id=self.run_id,
            task_id=self.task_id,
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            pending_forms=self.pending_forms,
            paused_nodes=self.paused_nodes,
            reported_usage=self.reported_usage,
        )

    def to_message(self) -> Message:
        return Message(
            answer="".join(self.text),
            message_id=self.message_id,
            conversation_id=self.conversation_id,
            task_id=self.task_id,
            metadata=self.metadata,
            created_at=self.created_at,
            error=self.error,
            executions=self.executions,
            pending_forms=self.pending_forms,
            finished=self.finished,
            replaced_reason=self.replaced_reason,
        )


class _BaseStream:
    """Shared plumbing for the sync streams."""

    def __init__(self, response: httpx.Response, *, raise_on_error: bool = True):
        self._response = response
        self._raise_on_error = raise_on_error
        self._watcher = _Watcher()
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop watching. The run on Dify keeps going — stop it with
        ``runs.stop()`` if that is what you meant."""
        if not self._closed:
            self._closed = True
            self._response.close()

    def __iter__(self) -> Iterator[RunEvent]:
        from .exceptions import APIError

        try:
            for line in self._response.iter_lines():
                payload = decode(line)
                if payload is None:
                    continue
                seen = _to_run_event(payload)
                self._watcher.see(seen)
                if self._raise_on_error and seen.type == "error":
                    message = seen.get("message") or seen.get("code") or "stream error"
                    raise APIError(str(message), seen.get("status", 500), seen.payload)
                yield seen
        finally:
            self.close()

    def text(self) -> Iterator[str]:
        """Just the answer, a piece at a time."""
        for event in self:
            if event.text:
                yield event.text


class WorkflowRunStream(_BaseStream):
    """A workflow run, watched as it happens.

    ::

        with app.workflows.runs.stream(inputs, user="alice") as stream:
            for event in stream:
                if event.type == "node_finished":
                    print(event.execution.node_id, event.execution.status)
            run = stream.get_final_run()
    """

    def snapshot(self) -> WorkflowRun:
        """The run as it stands right now, mid-stream.

        Legitimate at any moment, and not the same thing as the final result:
        a run still going has no outputs yet, which is different from a run
        that finished without any.
        """
        return self._watcher.to_run()

    def get_final_run(self) -> WorkflowRun:
        """What the run came to, once the stream is done.

        A run that paused reports ``paused``, not ``succeeded``. Called before
        the stream is exhausted this returns the same thing as
        :meth:`snapshot`, which is rarely what "final" was meant to mean — so
        iterate first.
        """
        return self._watcher.to_run()


class MessageStream(_BaseStream):
    """A chat message, watched as it is written.

    ::

        with app.chat.messages.stream(query="Hello", user="alice") as stream:
            for piece in stream.text():
                print(piece, end="", flush=True)
            message = stream.get_final_message()
    """

    def snapshot(self) -> Message:
        """The answer so far, mid-stream. ``finished`` is False until Dify
        says the message is done."""
        return self._watcher.to_message()

    def get_final_message(self) -> Message:
        """The finished message, once the stream is done.

        An answer that stopped arriving halfway comes back with
        ``finished=False`` and ``succeeded=False`` rather than passing for a
        complete one.
        """
        return self._watcher.to_message()


class _BaseAsyncStream:
    def __init__(self, response: httpx.Response, *, raise_on_error: bool = True):
        self._response = response
        self._raise_on_error = raise_on_error
        self._watcher = _Watcher()
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Stop watching. The run on Dify keeps going."""
        if not self._closed:
            self._closed = True
            await self._response.aclose()

    async def __aiter__(self) -> AsyncIterator[RunEvent]:
        from .exceptions import APIError

        try:
            async for line in self._response.aiter_lines():
                payload = decode(line)
                if payload is None:
                    continue
                seen = _to_run_event(payload)
                self._watcher.see(seen)
                if self._raise_on_error and seen.type == "error":
                    message = seen.get("message") or seen.get("code") or "stream error"
                    raise APIError(str(message), seen.get("status", 500), seen.payload)
                yield seen
        finally:
            await self.aclose()

    async def text(self) -> AsyncIterator[str]:
        async for event in self:
            if event.text:
                yield event.text


class AsyncWorkflowRunStream(_BaseAsyncStream):
    """The async counterpart of :class:`WorkflowRunStream`."""

    def snapshot(self) -> WorkflowRun:
        return self._watcher.to_run()

    def get_final_run(self) -> WorkflowRun:
        return self._watcher.to_run()


class AsyncMessageStream(_BaseAsyncStream):
    """The async counterpart of :class:`MessageStream`."""

    def snapshot(self) -> Message:
        return self._watcher.to_message()

    def get_final_message(self) -> Message:
        return self._watcher.to_message()


__all__ += ["AsyncMessageStream", "AsyncWorkflowRunStream"]
