"""Conversations: the threads a user has with this app."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, List, Literal

from ..paging import by_cursor, by_cursor_async, newest
from ..results import AsyncPage, Page
from ._base import Resource

SortBy = Literal["created_at", "-created_at", "updated_at", "-updated_at"]


@dataclass(frozen=True)
class Conversation:
    """One thread, belonging to one user."""

    id: str
    name: str = ""
    status: str = ""
    introduction: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    created_at: int | None = None
    updated_at: int | None = None


def _conversation(payload: Mapping[str, Any]) -> Conversation:
    return Conversation(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        status=str(payload.get("status") or ""),
        introduction=str(payload.get("introduction") or ""),
        inputs=dict(payload.get("inputs") or {}),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
    )


class Conversations(Resource):
    """A user's threads with this app."""

    def list(
        self,
        *,
        user: str | None = None,
        last_id: str | None = None,
        limit: int = 20,
        sort_by: SortBy = "-updated_at",
    ) -> Page[Conversation]:
        """List a user's conversations. Pages by cursor, not page number.

        ``page.all()`` walks every page; ``page.next_page()`` takes one step.
        """

        def fetch(cursor: str):
            return self._client._send_request(
                "GET",
                "/conversations",
                params={
                    "user": self._who(user),
                    "last_id": cursor or last_id,
                    "limit": limit,
                    "sort_by": sort_by,
                },
            ).json()

        # Newest first, so the next page continues from the last id on this
        # one — which is what Dify's `last_id` means.
        return by_cursor(fetch(""), _conversation, fetch, newest)

    def rename(
        self,
        conversation: Conversation | str,
        name: str | None = None,
        *,
        user: str | None = None,
        auto_generate: bool = False,
    ) -> Conversation:
        """Rename a thread, or have Dify name it from its contents."""
        cid = conversation if isinstance(conversation, str) else conversation.id
        body: dict[str, Any] = {"user": self._who(user), "auto_generate": auto_generate}
        if name is not None:
            body["name"] = name
        payload = self._client._send_request(
            "POST", f"/conversations/{cid}/name", body
        ).json()
        return _conversation(payload)

    def delete(
        self, conversation: Conversation | str, *, user: str | None = None
    ) -> None:
        """Delete a thread and its messages."""
        cid = conversation if isinstance(conversation, str) else conversation.id
        self._client._send_request(
            "DELETE", f"/conversations/{cid}", {"user": self._who(user)}
        )

    def set_variable(
        self,
        conversation: Conversation | str,
        variable_id: str,
        value: Any,
        *,
        user: str | None = None,
    ) -> dict[str, Any]:
        """Change one variable a thread is carrying.

        Conversation variables persist across turns, which is how a chatflow
        remembers what it was told earlier. Writing one is how a caller seeds
        or corrects that memory from outside.
        """
        cid = conversation if isinstance(conversation, str) else conversation.id
        return self._client._send_request(
            "PUT",
            f"/conversations/{cid}/variables/{variable_id}",
            {"value": value, "user": self._who(user)},
        ).json()

    def variables(
        self,
        conversation: Conversation | str,
        *,
        user: str | None = None,
        last_id: str | None = None,
        limit: int = 20,
        name: str | None = None,
    ) -> List[dict[str, Any]]:
        """The variables a thread has accumulated."""
        cid = conversation if isinstance(conversation, str) else conversation.id
        params: dict[str, Any] = {"user": self._who(user), "limit": limit}
        if last_id:
            params["last_id"] = last_id
        if name:
            params["variable_name"] = name
        payload = self._client._send_request(
            "GET", f"/conversations/{cid}/variables", params=params
        ).json()
        return list(payload.get("data", []))


class AsyncConversations(Resource):
    """The async counterpart of :class:`Conversations`."""

    async def list(
        self,
        *,
        user: str | None = None,
        last_id: str | None = None,
        limit: int = 20,
        sort_by: SortBy = "-updated_at",
    ) -> AsyncPage[Conversation]:
        """List a user's conversations. ``async for t in page.all()`` walks
        every page; ``await page.next_page()`` takes one step."""

        async def fetch(cursor: str) -> dict[str, Any]:
            response = await self._client._send_request(
                "GET",
                "/conversations",
                params={
                    "user": self._who(user),
                    "last_id": cursor or last_id,
                    "limit": limit,
                    "sort_by": sort_by,
                },
            )
            return dict(response.json())

        return by_cursor_async(await fetch(""), _conversation, fetch, newest)

    async def delete(
        self, conversation: Conversation | str, *, user: str | None = None
    ) -> None:
        cid = conversation if isinstance(conversation, str) else conversation.id
        await self._client._send_request(
            "DELETE", f"/conversations/{cid}", {"user": self._who(user)}
        )

    async def rename(
        self,
        conversation: Conversation | str,
        name: str | None = None,
        *,
        user: str | None = None,
        auto_generate: bool = False,
    ) -> Conversation:
        """Rename a thread, or have Dify name it from its contents."""
        cid = conversation if isinstance(conversation, str) else conversation.id
        body: dict[str, Any] = {"user": self._who(user), "auto_generate": auto_generate}
        if name is not None:
            body["name"] = name
        payload = (
            await self._client._send_request("POST", f"/conversations/{cid}/name", body)
        ).json()
        return _conversation(payload)

    async def variables(
        self,
        conversation: Conversation | str,
        *,
        user: str | None = None,
        last_id: str | None = None,
        limit: int = 20,
        name: str | None = None,
    ) -> List[dict[str, Any]]:
        """The variables a thread has accumulated."""
        cid = conversation if isinstance(conversation, str) else conversation.id
        params: dict[str, Any] = {"user": self._who(user), "limit": limit}
        if last_id:
            params["last_id"] = last_id
        if name:
            params["variable_name"] = name
        payload = (
            await self._client._send_request(
                "GET", f"/conversations/{cid}/variables", params=params
            )
        ).json()
        return list(payload.get("data", []))

    async def set_variable(
        self,
        conversation: Conversation | str,
        variable_id: str,
        value: Any,
        *,
        user: str | None = None,
    ) -> dict[str, Any]:
        cid = conversation if isinstance(conversation, str) else conversation.id
        return (
            await self._client._send_request(
                "PUT",
                f"/conversations/{cid}/variables/{variable_id}",
                {"value": value, "user": self._who(user)},
            )
        ).json()
