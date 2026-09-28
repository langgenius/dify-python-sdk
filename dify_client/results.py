"""What a run produced, wherever it ran.

These types live outside :mod:`dify_client.workflow` on purpose: they describe
a run's *outcome*, which the Service API reports too, and importing them must
not pull in graphon. Before this move, ``RunResult`` could not be imported at
all without the ``workflow`` extra installed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from .usage import Usage

_T = TypeVar("_T")

#: How many items :meth:`Page.all` walks before it stops and says so.
#:
#: A walk is a loop the server controls: it ends when Dify stops saying
#: ``has_more``. A server that never stops — a bug, a proxy, a listing growing
#: faster than it is read — would otherwise be an unbounded number of requests
#: inside what reads like an ordinary ``for``. Reaching this raises rather than
#: truncating, because a walk that quietly stops early is the bug ``all()`` was
#: written to fix.
MAX_WALK = 10_000


class PageLimitReached(RuntimeError):
    """A listing kept going past :data:`MAX_WALK` items.

    Carries what was read, so the walk does not have to be repeated::

        try:
            everything = list(page.all())
        except PageLimitReached as reached:
            everything = reached.items       # the first MAX_WALK of them
    """

    def __init__(self, items: list[Any], limit: int):
        self.items = items
        self.limit = limit
        super().__init__(
            f"Walked {limit:,} items and Dify still says there are more. "
            "Pass all(max_items=…) if the listing really is this long, or "
            "narrow it with the arguments the listing takes."
        )


#: Statuses Dify uses for a run that is over, however it went.
_SETTLED = frozenset({"succeeded", "failed", "stopped", "partial-succeeded"})


def form_tokens(source: Any) -> list[str]:
    """The human-input forms a paused answer is waiting on.

    A blocking run that pauses reports ``data.reasons``, each with its own
    ``form_token`` — the token :meth:`dify_client.resources.forms.Forms.submit`
    answers with. Streaming raises the same tokens one event at a time; this is
    the same information arriving all at once, and dropping it left a caller
    holding a paused run with no way to resume it.
    """
    data = source.get("data") if isinstance(source, dict) else None
    reasons = (data if isinstance(data, dict) else source or {}).get("reasons")
    if not isinstance(reasons, list):
        return []
    tokens = []
    for reason in reasons:
        token = (
            str((reason or {}).get("form_token") or "")
            if isinstance(reason, dict)
            else ""
        )
        if token:
            tokens.append(token)
    return tokens


class WorkflowRunError(Exception):
    """Raised when a local workflow run fails and the caller wanted a value."""


@dataclass
class NodeExecution:
    """One execution of one node.

    Distinct from the node itself: a node inside an iteration or a loop runs
    once per item, and each pass is its own execution with its own usage.
    Treating the two as the same thing is what made a looped node report only
    its last pass.
    """

    node_id: str
    node_type: str
    status: str
    #: Dify's id for *this execution*, distinct from ``node_id``, which names
    #: the node in the graph. A node in a loop shares one node_id across every
    #: pass and has a different execution_id each time — which is what makes
    #: reconnecting to a stream able to tell a repeat from a resend.
    execution_id: str = ""
    #: Where this execution came in the run. Repeats share a node_id, so the
    #: order is what puts them back in sequence.
    index: int = 0
    title: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    process_data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    usage: Usage = field(default_factory=Usage)

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    def __getitem__(self, key: str) -> Any:
        return self.outputs[key]

    def __str__(self) -> str:
        return f"{self.node_id} ({self.status})"


@dataclass
class WorkflowRun:
    """One run of a workflow, wherever it ran.

    The same type comes back from a local run and from Dify, so a test written
    against one reads the same against the other. What differs is what Dify
    knows about: ``run_id`` and ``task_id`` are empty for a local run, which
    Dify never saw.

    ``outputs`` holds the workflow-level result; ``node()`` reaches into any
    one node so a test can assert on the middle of the graph, not just the end.
    """

    status: str
    outputs: dict[str, Any] = field(default_factory=dict)
    nodes: dict[str, NodeExecution] = field(default_factory=dict)
    error: str = ""
    stream: list[str] = field(default_factory=list)
    #: Every node execution in order, including repeats. A node inside an
    #: iteration or loop runs once per item and appears here once per run;
    #: ``nodes`` keeps only the last, keyed by node id.
    executions: list[NodeExecution] = field(default_factory=list)
    #: Dify's id for this run. Needed to reopen its event stream, and to read
    #: it back from the logs. Empty for a local run, which Dify never saw.
    run_id: str = ""
    #: The id a stop request names. Dify sends it on the first event.
    task_id: str = ""
    #: The conversation this run belongs to, for a chatflow. Pass it back to
    #: continue the thread.
    conversation_id: str = ""
    #: The message this run produced, for a chatflow. What feedback attaches to.
    message_id: str = ""
    #: Tokens of any human-input forms this run paused on, in the order they
    #: were raised.
    pending_forms: list[str] = field(default_factory=list)
    #: Nodes the run is waiting at. Not the same list as ``pending_forms``:
    #: Dify leaves the token out of a form it means to be answered in its own
    #: UI, so a run can be waiting at a node with no token to submit against.
    paused_nodes: list[str] = field(default_factory=list)
    #: What Dify said the *whole run* consumed, when it said anything.
    #:
    #: Separate from the per-node figures on purpose. A blocking response
    #: reports only this; a stream reports only the breakdown; and reconnecting
    #: to a finished run reports this alone, with no node events at all — so
    #: summing executions would have called a 123-token run free.
    reported_usage: Usage | None = None

    @property
    def usage(self) -> Usage:
        """What the run consumed.

        Merged from two sources, because neither is complete. Dify's figure for
        the whole run covers parts this client never watched — reconnecting to a
        finished run reports the total and no node events at all — but carries
        no price: ``workflow_finished`` has ``total_tokens`` and nothing else.
        The per-node figures carry both, for the nodes that were seen.

        So the token count comes from the run total when there is one, and the
        cost from whichever reported one. Preferring the run total wholesale
        threw the price away on every streamed run.

        Otherwise this sums ``executions``, so a node inside a loop contributes
        once per pass. Summing ``nodes`` instead undercounted: a node that ran
        three times was billed three times but reported once, and a budget set
        with ``max_tokens`` would pass a run that had already gone over.

        A run answered by a stub reports nothing, which is what makes
        "this test spent nothing" an assertion rather than a belief.
        """
        observed = self.node_usage
        if self.reported_usage is None:
            return observed
        return observed.merged_with(self.reported_usage)

    @property
    def node_usage(self) -> Usage:
        """Summed over the node executions this client actually observed.

        Lower than :attr:`usage` when the stream was joined late or reopened —
        the difference is what happened while nobody was watching.
        """
        total = Usage()
        for node in self.executions or self.nodes.values():
            total = total + node.usage
        return total

    def runs_of(self, node_id: str) -> list[NodeExecution]:
        """Every execution of one node, in order.

        A node outside a loop has exactly one. Inside an iteration or a loop it
        has one per pass, which ``node()`` cannot show.
        """
        return [n for n in self.executions if n.node_id == node_id]

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def paused(self) -> bool:
        """Whether the run stopped waiting for a person to fill in a form.

        Waiting is not failing: a paused run has neither succeeded nor failed,
        and resumes when the form is submitted. ``succeeded`` is False here,
        and so is ``failed`` — the three are separate questions.

        Dify's own word is what settles it. Reading this off ``pending_forms``
        alone answered False for a run read back with ``retrieve()``, which
        reports ``status="paused"`` and no forms at all — the pause was
        visible to this client only if it had watched the stream that raised
        the form.
        """
        if self.status == "paused":
            return True
        waiting = bool(self.pending_forms) or bool(self.paused_nodes)
        return waiting and self.status not in _SETTLED

    @property
    def failed(self) -> bool:
        """Whether Dify reported the run as failed.

        Distinct from a dropped connection, which says nothing about the run,
        and from a pause, which is the run waiting rather than stopping.
        """
        return self.status == "failed"

    @property
    def finished(self) -> bool:
        """Whether the run reached an end state, either way."""
        return self.status in _SETTLED

    def node(self, node_id: str) -> NodeExecution:
        """Return the result of one node, by id."""
        try:
            return self.nodes[node_id]
        except KeyError:
            known = ", ".join(sorted(self.nodes)) or "none"
            msg = f"Node {node_id!r} did not run. Nodes that ran: {known}."
            raise KeyError(msg) from None

    def __getitem__(self, key: str) -> Any:
        return self.outputs[key]

    def raise_for_status(self) -> RunResult:
        """Raise if the run did not succeed, otherwise return self."""
        if not self.succeeded:
            failed = [n for n in self.nodes.values() if n.error]
            detail = self.error or "; ".join(f"{n.node_id}: {n.error}" for n in failed)
            msg = f"Workflow run {self.status}: {detail or 'no error detail'}"
            raise WorkflowRunError(msg)
        return self


#: Names these types had when they only described local runs.
NodeResult = NodeExecution
RunResult = WorkflowRun


@dataclass
class Message:
    """One message a chat app produced, and the thread it belongs to.

    The counterpart of :class:`WorkflowRun` for the conversational modes. Both
    carry the ids the next call needs: ``conversation_id`` continues the
    thread, ``message_id`` is what feedback attaches to, and ``task_id`` is
    what a stop request names.
    """

    answer: str = ""
    message_id: str = ""
    conversation_id: str = ""
    task_id: str = ""
    #: Dify's own metadata block — retriever resources, usage, and whatever a
    #: newer Dify adds. Kept whole rather than picked apart, so a field this
    #: SDK has not heard of still reaches the caller.
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: int | None = None
    error: str = ""
    #: Node executions, for a chatflow. An ordinary chat app reports none.
    executions: list[NodeExecution] = field(default_factory=list)
    #: Tokens of human-input forms this message is *still* waiting on. A form
    #: that has been answered is removed, so a resumed message is not reported
    #: as paused forever.
    pending_forms: list[str] = field(default_factory=list)
    #: Whether Dify said the message was finished. A stream cut short mid-answer
    #: never sets this, which is what separates "the answer so far" from "the
    #: answer" — both are legitimate readings of a stream, and they are not the
    #: same thing.
    finished: bool = False
    #: Why output moderation replaced the answer, when it did. ``answer`` is
    #: then the replacement — what Dify saved — not what the model wrote.
    replaced_reason: str = ""

    @property
    def usage(self) -> Usage:
        """What this message cost, from Dify's metadata or its node runs."""
        reported = self.metadata.get("usage")
        if isinstance(reported, dict) and reported:
            return Usage.from_metadata(reported)
        total = Usage()
        for execution in self.executions:
            total = total + execution.usage
        return total

    @property
    def paused(self) -> bool:
        """Whether a chatflow is waiting for a person to fill in a form."""
        return bool(self.pending_forms)

    @property
    def succeeded(self) -> bool:
        """Whether Dify finished this message without an error.

        An answer that stopped arriving halfway is not a success: the stream
        ended, ``finished`` was never set, and calling that succeeded is how a
        truncated reply gets stored as a complete one.
        """
        return self.finished and not self.error and not self.paused

    def __str__(self) -> str:
        return self.answer


@dataclass(frozen=True)
class HistoryMessage:
    """One turn in a conversation, as Dify records it.

    Deliberately not a :class:`Message`. A message you just generated and a
    turn read back from history are different things: history carries the
    ``query`` that prompted the answer, the files attached, the feedback left
    and what it cost — none of which a fresh reply has — and it has no
    ``task_id``, because nothing is running to stop.

    Reusing one type for both dropped the query, which made a conversation
    impossible to reconstruct: every answer, and nothing anyone asked.
    """

    id: str
    conversation_id: str = ""
    #: What the user said. The half that a generated reply does not carry.
    query: str = ""
    answer: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    #: ``like``, ``dislike``, or None when nobody rated it.
    feedback: str | None = None
    retriever_resources: list[dict[str, Any]] = field(default_factory=list)
    agent_thoughts: list[dict[str, Any]] = field(default_factory=list)
    status: str = ""
    error: str = ""
    created_at: int | None = None
    usage: Usage = field(default_factory=Usage)
    #: Everything Dify sent, so a field this SDK does not name is not lost.
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def __str__(self) -> str:
        return self.answer


@dataclass(frozen=True)
class Page(Generic[_T]):
    """One page of results, and how to get the rest.

    Dify pages two ways and this hides neither: some listings take a ``page``
    number, others a cursor (``last_id`` or ``first_id``). What they share is
    what a caller needs — whether there is more, how to get it, and how to walk
    the lot — so that is what this offers.

    Iterating a page gives its items, so it reads like the list it replaced::

        page = app.chat.conversations.list()
        for thread in page:            # this page
            ...
        for thread in page.all():      # every page, fetched as needed
            ...
    """

    items: list[_T] = field(default_factory=list)
    #: Whether Dify said there is another page. False is not "this page was
    #: short" — a short last page and a short only page look the same without
    #: it, which is why dropping it made callers guess.
    has_more: bool = False
    limit: int = 0
    total: int | None = None
    #: Fetches the next page, or None when this listing cannot be continued.
    #: Not compared or printed: it is plumbing, not content.
    _next: Callable[[], Page[_T]] | None = field(
        default=None, repr=False, compare=False
    )

    def __iter__(self) -> Iterator[_T]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> _T:
        return self.items[index]

    def __bool__(self) -> bool:
        return bool(self.items)

    def next_page(self) -> Page[_T] | None:
        """The page after this one, or None when there is not one.

        None also when this listing does not support being continued — Dify
        answers a few with a bare array and no cursor at all.
        """
        if not self.has_more or self._next is None:
            return None
        return self._next()

    def all(self, *, max_items: int | None = None) -> Iterator[_T]:
        """Every item across every page, fetching as it goes.

        The loop that callers were writing by hand, and the reason a "find by
        name" helper used to look at only the first hundred and report a real
        app as missing.

        Args:
            max_items: How far to walk before giving up on a server that never
                says stop. Defaults to :data:`MAX_WALK`. Raising the ceiling is
                a decision; there is no "no limit".
        """
        ceiling = MAX_WALK if max_items is None else max_items
        walked: list[_T] = []
        page: Page[_T] | None = self
        while page is not None:
            for item in page.items:
                if len(walked) >= ceiling and page.has_more:
                    raise PageLimitReached(walked, ceiling)
                walked.append(item)
                yield item
            if page.has_more and not page.items:
                # Dify says there is more and sends nothing: stop rather than
                # ask forever.
                return
            page = page.next_page()

    def pages(self) -> Iterator[Page[_T]]:
        """This page and the ones after it."""
        page: Page[_T] | None = self
        while page is not None:
            yield page
            page = page.next_page()


@dataclass(frozen=True)
class AsyncPage(Generic[_T]):
    """One page of results from an async client, and how to get the rest.

    The same thing as :class:`Page` with awaits where the network is. It is a
    separate type rather than a mode of ``Page`` because the difference cannot
    be hidden: ``page.next_page()`` either blocks or returns a coroutine, and a
    single type that did both would be a trap in whichever context it was not
    written for.

    Iterating gives this page's items; ``all()`` walks every page::

        page = await app.chat.conversations.list()
        for thread in page:                # this page, already fetched
            ...
        async for thread in page.all():    # every page, fetched as needed
            ...
    """

    items: list[_T] = field(default_factory=list)
    has_more: bool = False
    limit: int = 0
    total: int | None = None
    _next: Callable[[], Awaitable[AsyncPage[_T]]] | None = field(
        default=None, repr=False, compare=False
    )

    def __iter__(self) -> Iterator[_T]:
        """This page's items. Already fetched, so no await is involved."""
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> _T:
        return self.items[index]

    def __bool__(self) -> bool:
        return bool(self.items)

    async def next_page(self) -> AsyncPage[_T] | None:
        """The page after this one, or None when there is not one."""
        if not self.has_more or self._next is None:
            return None
        return await self._next()

    async def all(self, *, max_items: int | None = None) -> AsyncIterator[_T]:
        """Every item across every page, fetching as it goes.

        Stops at :data:`MAX_WALK` items, the same way :meth:`Page.all` does.
        """
        ceiling = MAX_WALK if max_items is None else max_items
        walked: list[_T] = []
        page: AsyncPage[_T] | None = self
        while page is not None:
            for item in page.items:
                if len(walked) >= ceiling and page.has_more:
                    raise PageLimitReached(walked, ceiling)
                walked.append(item)
                yield item
            if page.has_more and not page.items:
                return
            page = await page.next_page()

    async def pages(self) -> AsyncIterator[AsyncPage[_T]]:
        """This page and the ones after it."""
        page: AsyncPage[_T] | None = self
        while page is not None:
            yield page
            page = await page.next_page()
