"""Messages: sending one, watching it written, and reading a thread back."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, List, Literal

from ..paging import by_cursor, by_cursor_async, oldest
from ..results import AsyncPage, HistoryMessage, Message, Page, form_tokens
from ..streams import AsyncMessageStream, MessageStream
from ..usage import Usage
from ._base import Resource


def _message_from_blocking(payload: Mapping[str, Any]) -> Message:
    """A blocking reply is the finished message by definition.

    Unlike a stream, which can stop mid-answer, this arrives only once Dify has
    the whole thing — so `finished` is true, and the message is not mistaken
    for a truncated one.

    Except when it is not finished at all: a chatflow that reaches a
    human-input node answers ``event: "workflow_paused"`` with the same fields
    plus the forms it is waiting on. That is a message still being written, and
    saying `finished` of it would make `succeeded` true for an answer nobody
    has given yet.
    """
    waiting = form_tokens(payload)
    return Message(
        finished=not waiting,
        pending_forms=waiting,
        answer=str(payload.get("answer") or ""),
        message_id=str(payload.get("message_id") or payload.get("id") or ""),
        conversation_id=str(payload.get("conversation_id") or ""),
        task_id=str(payload.get("task_id") or ""),
        metadata=dict(payload.get("metadata") or {}),
        created_at=payload.get("created_at"),
    )


def _history(payload: Mapping[str, Any]) -> HistoryMessage:
    feedback = payload.get("feedback")
    return HistoryMessage(
        id=str(payload.get("id") or ""),
        conversation_id=str(payload.get("conversation_id") or ""),
        query=str(payload.get("query") or ""),
        answer=str(payload.get("answer") or ""),
        inputs=dict(payload.get("inputs") or {}),
        files=list(payload.get("message_files") or []),
        feedback=(feedback or {}).get("rating") if isinstance(feedback, dict) else None,
        retriever_resources=list(payload.get("retriever_resources") or []),
        agent_thoughts=list(payload.get("agent_thoughts") or []),
        status=str(payload.get("status") or ""),
        error=str(payload.get("error") or ""),
        created_at=payload.get("created_at"),
        usage=Usage.from_metadata(
            {
                "prompt_tokens": payload.get("message_tokens"),
                "completion_tokens": payload.get("answer_tokens"),
                "total_tokens": payload.get("total_tokens"),
                "total_price": payload.get("total_price"),
                "currency": payload.get("currency"),
            },
            payload.get("provider_response_latency"),
        ),
        payload=dict(payload),
    )


class Messages(Resource):
    """Messages in this app's conversations.

    ``create`` waits for the whole answer. ``stream`` hands it back as it is
    written. Both return the thread's ``conversation_id``, which is what
    continues it.
    """

    def create(
        self,
        query: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        user: str | None = None,
        conversation_id: str | None = None,
        files: Any = None,
    ) -> Message:
        """Send a message and wait for the answer.

        Args:
            query: What the user said. A chat app needs this; the start-node
                variables go in ``inputs``.
            inputs: Start-node variables, if the app declares any.
            user: End-user identifier.
            conversation_id: Continue this thread. Left out, a new one starts
                and its id comes back on the message.
            files: Files to attach, in Dify's file mapping shape.
        """
        body: dict[str, Any] = {
            "query": query,
            "inputs": dict(inputs or {}),
            "response_mode": "blocking",
            "user": self._who(user),
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        if files is not None:
            body["files"] = files
        response = self._client._send_request("POST", "/chat-messages", body)
        return _message_from_blocking(response.json())

    def stream(
        self,
        query: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        user: str | None = None,
        conversation_id: str | None = None,
        files: Any = None,
        raise_on_error: bool = True,
    ) -> MessageStream:
        """Send a message and watch the answer arrive::

        with app.chat.messages.stream("Hello") as stream:
            for piece in stream.text():
                print(piece, end="", flush=True)
            message = stream.get_final_message()
        """
        body: dict[str, Any] = {
            "query": query,
            "inputs": dict(inputs or {}),
            "response_mode": "streaming",
            "user": self._who(user),
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        if files is not None:
            body["files"] = files
        response = self._client._send_request(
            "POST", "/chat-messages", body, stream=True
        )
        return MessageStream(response, raise_on_error=raise_on_error)

    def list(
        self,
        conversation_id: str,
        *,
        user: str | None = None,
        first_id: str | None = None,
        limit: int = 20,
    ) -> Page[HistoryMessage]:
        """One conversation's turns, newest page first.

        Returns :class:`~dify_client.results.HistoryMessage`, not ``Message``:
        history carries the ``query`` that prompted each answer, the files and
        the feedback, and has no running task to stop. Treating them as the
        same type dropped the query, leaving a transcript of answers to
        questions nobody could see.

        Each page arrives oldest-first, and the page after it is *older* — so
        ``all()`` continues from the first id on each page. One thing this
        cannot paper over: Dify's cursor compares ``created_at``, which it
        stores to the second, and excludes ties. Messages written inside the
        same second can therefore be skipped by any client that pages this
        listing, this one included. Read such a conversation in one page
        (``limit`` above its length) if that matters.
        """

        def fetch(cursor: str):
            return self._client._send_request(
                "GET",
                "/messages",
                params={
                    "conversation_id": conversation_id,
                    "user": self._who(user),
                    "first_id": cursor or first_id,
                    "limit": limit,
                },
            ).json()

        # History pages backwards: a page arrives oldest-first and the next
        # one is older still, so it continues from the *first* id here. Taking
        # the last one asked for everything older than the newest message on
        # the page — most of the page again, every time.
        return by_cursor(fetch(""), _history, fetch, oldest)

    def stop(self, message: Message | str, *, user: str | None = None) -> None:
        """Stop an answer that is still being written."""
        task_id = message if isinstance(message, str) else message.task_id
        if not task_id:
            from ..exceptions import ValidationError

            msg = (
                "This message carries no task_id, so there is nothing to stop. "
                "Only a streamed message reports one."
            )
            raise ValidationError(msg)
        self._client._send_request(
            "POST", f"/chat-messages/{task_id}/stop", {"user": self._who(user)}
        )

    def feedback(
        self,
        message: Message | str,
        rating: Literal["like", "dislike"] | None,
        *,
        user: str | None = None,
        content: str | None = None,
    ) -> None:
        """Rate a message, or take a rating back with ``rating=None``."""
        message_id = message if isinstance(message, str) else message.message_id
        body: dict[str, Any] = {"rating": rating, "user": self._who(user)}
        if content is not None:
            body["content"] = content
        self._client._send_request("POST", f"/messages/{message_id}/feedbacks", body)

    def suggested(
        self, message: Message | str, *, user: str | None = None
    ) -> List[str]:
        """What Dify suggests the user might ask next."""
        message_id = message if isinstance(message, str) else message.message_id
        payload = self._client._send_request(
            "GET",
            f"/messages/{message_id}/suggested",
            params={"user": self._who(user)},
        ).json()
        data = payload.get("data")
        return [str(item) for item in data] if isinstance(data, list) else []


class AsyncMessages(Resource):
    """The async counterpart of :class:`Messages`."""

    async def create(
        self,
        query: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        user: str | None = None,
        conversation_id: str | None = None,
        files: Any = None,
    ) -> Message:
        body: dict[str, Any] = {
            "query": query,
            "inputs": dict(inputs or {}),
            "response_mode": "blocking",
            "user": self._who(user),
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        if files is not None:
            body["files"] = files
        response = await self._client._send_request("POST", "/chat-messages", body)
        return _message_from_blocking(response.json())

    async def stream(
        self,
        query: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        user: str | None = None,
        conversation_id: str | None = None,
        files: Any = None,
        raise_on_error: bool = True,
    ) -> AsyncMessageStream:
        body: dict[str, Any] = {
            "query": query,
            "inputs": dict(inputs or {}),
            "response_mode": "streaming",
            "user": self._who(user),
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        if files is not None:
            body["files"] = files
        response = await self._client._send_request(
            "POST", "/chat-messages", body, stream=True
        )
        return AsyncMessageStream(response, raise_on_error=raise_on_error)

    async def feedback(
        self,
        message: Message | str,
        rating: Literal["like", "dislike"] | None,
        *,
        user: str | None = None,
        content: str | None = None,
    ) -> None:
        message_id = message if isinstance(message, str) else message.message_id
        body: dict[str, Any] = {"rating": rating, "user": self._who(user)}
        if content is not None:
            body["content"] = content
        await self._client._send_request(
            "POST", f"/messages/{message_id}/feedbacks", body
        )

    async def list(
        self,
        conversation_id: str,
        *,
        user: str | None = None,
        first_id: str | None = None,
        limit: int = 20,
    ) -> AsyncPage[HistoryMessage]:
        """One conversation's turns, newest page first.

        Returns :class:`~dify_client.results.HistoryMessage`, not ``Message``:
        history carries the ``query`` that prompted each answer, the files and
        the feedback, and has no running task to stop.
        """

        async def fetch(cursor: str) -> dict[str, Any]:
            response = await self._client._send_request(
                "GET",
                "/messages",
                params={
                    "conversation_id": conversation_id,
                    "user": self._who(user),
                    "first_id": cursor or first_id,
                    "limit": limit,
                },
            )
            return dict(response.json())

        # Oldest-first, continued from the first id. See the sync method.
        return by_cursor_async(await fetch(""), _history, fetch, oldest)

    async def stop(self, message: Message | str, *, user: str | None = None) -> None:
        """Stop an answer that is still being written."""
        task_id = message if isinstance(message, str) else message.task_id
        if not task_id:
            from ..exceptions import ValidationError

            msg = (
                "This message carries no task_id, so there is nothing to stop. "
                "Only a streamed message reports one."
            )
            raise ValidationError(msg)
        await self._client._send_request(
            "POST", f"/chat-messages/{task_id}/stop", {"user": self._who(user)}
        )

    async def suggested(
        self, message: Message | str, *, user: str | None = None
    ) -> List[str]:
        """What Dify suggests the user might ask next."""
        message_id = message if isinstance(message, str) else message.message_id
        payload = (
            await self._client._send_request(
                "GET",
                f"/messages/{message_id}/suggested",
                params={"user": self._who(user)},
            )
        ).json()
        data = payload.get("data")
        return [str(item) for item in data] if isinstance(data, list) else []
