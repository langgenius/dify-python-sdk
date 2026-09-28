"""Completions: one prompt in, one answer out, no thread."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..results import Message
from ..streams import AsyncMessageStream, MessageStream
from ._base import Resource
from .messages import _message_from_blocking


class Completions(Resource):
    """This app's completions. Only a ``completion``-mode app serves these."""

    def create(
        self,
        inputs: Mapping[str, Any] | None = None,
        *,
        user: str | None = None,
        files: Any = None,
    ) -> Message:
        """Send a prompt and wait for the answer.

        The prompt goes in ``inputs``, under whichever variable the app
        declares — usually ``query``. There is no conversation to continue.
        """
        body: dict[str, Any] = {
            "inputs": dict(inputs or {}),
            "response_mode": "blocking",
            "user": self._who(user),
        }
        if files is not None:
            body["files"] = files
        response = self._client._send_request("POST", "/completion-messages", body)
        return _message_from_blocking(response.json())

    def stream(
        self,
        inputs: Mapping[str, Any] | None = None,
        *,
        user: str | None = None,
        files: Any = None,
        raise_on_error: bool = True,
    ) -> MessageStream:
        """Send a prompt and watch the answer arrive."""
        body: dict[str, Any] = {
            "inputs": dict(inputs or {}),
            "response_mode": "streaming",
            "user": self._who(user),
        }
        if files is not None:
            body["files"] = files
        response = self._client._send_request(
            "POST", "/completion-messages", body, stream=True
        )
        return MessageStream(response, raise_on_error=raise_on_error)

    def stop(self, message: Message | str, *, user: str | None = None) -> None:
        """Stop a completion that is still being written."""
        task_id = message if isinstance(message, str) else message.task_id
        if not task_id:
            from ..exceptions import ValidationError

            msg = (
                "This completion carries no task_id, so there is nothing to "
                "stop. Only a streamed one reports one."
            )
            raise ValidationError(msg)
        self._client._send_request(
            "POST", f"/completion-messages/{task_id}/stop", {"user": self._who(user)}
        )


class AsyncCompletions(Resource):
    """The async counterpart of :class:`Completions`."""

    async def create(
        self,
        inputs: Mapping[str, Any] | None = None,
        *,
        user: str | None = None,
        files: Any = None,
    ) -> Message:
        body: dict[str, Any] = {
            "inputs": dict(inputs or {}),
            "response_mode": "blocking",
            "user": self._who(user),
        }
        if files is not None:
            body["files"] = files
        response = await self._client._send_request(
            "POST", "/completion-messages", body
        )
        return _message_from_blocking(response.json())

    async def stream(
        self,
        inputs: Mapping[str, Any] | None = None,
        *,
        user: str | None = None,
        files: Any = None,
        raise_on_error: bool = True,
    ) -> AsyncMessageStream:
        body: dict[str, Any] = {
            "inputs": dict(inputs or {}),
            "response_mode": "streaming",
            "user": self._who(user),
        }
        if files is not None:
            body["files"] = files
        response = await self._client._send_request(
            "POST", "/completion-messages", body, stream=True
        )
        return AsyncMessageStream(response, raise_on_error=raise_on_error)

    async def stop(self, message: Message | str, *, user: str | None = None) -> None:
        """Stop a completion that is still being written."""
        task_id = message if isinstance(message, str) else message.task_id
        if not task_id:
            from ..exceptions import ValidationError

            msg = (
                "This completion carries no task_id, so there is nothing to "
                "stop. Only a streamed one reports one."
            )
            raise ValidationError(msg)
        await self._client._send_request(
            "POST", f"/completion-messages/{task_id}/stop", {"user": self._who(user)}
        )
