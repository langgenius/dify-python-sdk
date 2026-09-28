"""Turning Dify's two paging styles into one thing a caller can use.

Dify pages some listings by number and others by cursor, and a few not at all.
The difference is real and stays visible in the arguments — ``page=2`` and
``last_id=…`` mean different things — but what a caller does with a page should
not depend on which kind it is. These build a :class:`~dify_client.results.Page`
that knows how to fetch its own successor, whichever way that works.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

from .results import AsyncPage, Page

__all__ = [
    "by_cursor",
    "by_cursor_async",
    "by_page",
    "by_page_async",
    "newest",
    "oldest",
    "unpaged",
    "unpaged_async",
]

_T = TypeVar("_T")


def _envelope(payload: Mapping[str, Any]) -> tuple[list[Any], bool, int, int | None]:
    """The four things Dify's list envelopes carry, however they spell them."""
    data = payload.get("data")
    items = list(data) if isinstance(data, list) else []
    total = payload.get("total")
    return (
        items,
        bool(payload.get("has_more")),
        int(payload.get("limit") or 0),
        int(total) if total is not None else None,
    )


def by_page(
    payload: Mapping[str, Any],
    build: Callable[[Mapping[str, Any]], _T],
    fetch: Callable[[int], Mapping[str, Any]],
    page: int,
) -> Page[_T]:
    """A page of a numbered listing, able to fetch page ``page + 1``."""
    raw, has_more, limit, total = _envelope(payload)
    return Page(
        items=[build(item) for item in raw],
        has_more=has_more,
        limit=limit,
        total=total,
        _next=lambda: by_page(fetch(page + 1), build, fetch, page + 1),
    )


def by_cursor(
    payload: Mapping[str, Any],
    build: Callable[[Mapping[str, Any]], _T],
    fetch: Callable[[str], Mapping[str, Any]],
    cursor_from: Callable[[list[Any]], str],
    *,
    previous: str = "",
) -> Page[_T]:
    """A page of a cursor listing, able to fetch the page after it.

    ``cursor_from`` is handed the whole page and says which end of it
    continues the listing, because that is not the same for every one:
    conversations come newest-first and continue from the **last** id they
    carry, while a conversation's messages come oldest-first and continue
    from the **first**, since the next page is older still. Taking the last
    item either way re-fetched most of the page it had just read — the same
    messages over and over, which is what this signature exists to prevent.
    """
    raw, has_more, limit, total = _envelope(payload)
    cursor = cursor_from(raw) if raw else ""
    # A cursor that comes back unchanged is a listing that is not advancing:
    # asking again returns this page again. `all()` would then fetch it
    # forever — a hang rather than an error — so such a page simply reports
    # that it cannot be continued.
    advancing = bool(cursor) and cursor != previous
    return Page(
        items=[build(item) for item in raw],
        has_more=has_more,
        limit=limit,
        total=total,
        _next=(
            (
                lambda: by_cursor(
                    fetch(cursor), build, fetch, cursor_from, previous=cursor
                )
            )
            if advancing
            else None
        ),
    )


def unpaged(items: list[_T]) -> Page[_T]:
    """Everything, from a listing Dify does not page.

    Still a page, so a caller does not have to know which listings page and
    which do not — ``all()`` works either way and stops after one.
    """
    return Page(items=items, has_more=False, limit=len(items), total=len(items))


def by_page_async(
    payload: Mapping[str, Any],
    build: Callable[[Mapping[str, Any]], _T],
    fetch: Callable[[int], Awaitable[Mapping[str, Any]]],
    page: int,
) -> AsyncPage[_T]:
    """:func:`by_page`, for a client whose fetch is awaited."""
    raw, has_more, limit, total = _envelope(payload)

    async def advance() -> AsyncPage[_T]:
        return by_page_async(await fetch(page + 1), build, fetch, page + 1)

    return AsyncPage(
        items=[build(item) for item in raw],
        has_more=has_more,
        limit=limit,
        total=total,
        _next=advance,
    )


def by_cursor_async(
    payload: Mapping[str, Any],
    build: Callable[[Mapping[str, Any]], _T],
    fetch: Callable[[str], Awaitable[Mapping[str, Any]]],
    cursor_from: Callable[[list[Any]], str],
    *,
    previous: str = "",
) -> AsyncPage[_T]:
    """:func:`by_cursor`, for a client whose fetch is awaited."""
    raw, has_more, limit, total = _envelope(payload)
    cursor = cursor_from(raw) if raw else ""
    advancing = bool(cursor) and cursor != previous

    async def advance() -> AsyncPage[_T]:
        return by_cursor_async(
            await fetch(cursor), build, fetch, cursor_from, previous=cursor
        )

    return AsyncPage(
        items=[build(item) for item in raw],
        has_more=has_more,
        limit=limit,
        total=total,
        _next=advance if advancing else None,
    )


def unpaged_async(items: list[_T]) -> AsyncPage[_T]:
    """:func:`unpaged`, for a client whose other listings are awaited."""
    return AsyncPage(items=items, has_more=False, limit=len(items), total=len(items))


def oldest(items: list[Any]) -> str:
    """The id to continue from when a page arrives oldest-first.

    The next page is older than this one, so it continues from the *first*
    item — Dify's message history, whose cursor is spelled ``first_id``.
    """
    return str(items[0]["id"]) if items else ""


def newest(items: list[Any]) -> str:
    """The id to continue from when a page arrives newest-first.

    The next page is further down, so it continues from the *last* item —
    Dify's conversation listing, whose cursor is spelled ``last_id``.
    """
    return str(items[-1]["id"]) if items else ""
