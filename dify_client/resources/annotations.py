"""Annotations: answers you have written yourself, for Dify to reuse."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from ..paging import by_page, by_page_async
from ..results import AsyncPage, Page
from ._base import Resource


@dataclass(frozen=True)
class Annotation:
    """A question and the answer you want given for it."""

    id: str
    question: str = ""
    answer: str = ""
    hit_count: int = 0
    created_at: int | None = None


def _annotation(payload: Mapping[str, Any]) -> Annotation:
    return Annotation(
        id=str(payload.get("id") or ""),
        question=str(payload.get("question") or ""),
        answer=str(payload.get("answer") or ""),
        hit_count=int(payload.get("hit_count") or 0),
        created_at=payload.get("created_at"),
    )


@dataclass(frozen=True)
class AnnotationReplyJob:
    """The indexing job that turns annotations on or off.

    Enabling annotation reply embeds every annotation, which takes time — so
    Dify answers with a job rather than a result. ``status()`` polls it.
    """

    id: str
    status: str = ""
    error: str = ""

    @property
    def finished(self) -> bool:
        return self.status in {"completed", "failed", "error"}


class Annotations(Resource):
    """This app's annotations, and the reply setting that uses them."""

    def list(
        self, *, page: int = 1, limit: int = 20, keyword: str | None = None
    ) -> Page[Annotation]:
        """List the app's annotations."""

        def fetch(number: int):
            params: dict[str, Any] = {"page": number, "limit": limit}
            if keyword is not None:
                params["keyword"] = keyword
            return self._client._send_request(
                "GET", "/apps/annotations", params=params
            ).json()

        return by_page(fetch(page), _annotation, fetch, page)

    def create(self, question: str, answer: str) -> Annotation:
        """Add an annotation."""
        payload = self._client._send_request(
            "POST", "/apps/annotations", {"question": question, "answer": answer}
        ).json()
        return _annotation(payload)

    def update(
        self, annotation: Annotation | str, question: str, answer: str
    ) -> Annotation:
        """Replace an annotation's question and answer."""
        annotation_id = annotation if isinstance(annotation, str) else annotation.id
        payload = self._client._send_request(
            "PUT",
            f"/apps/annotations/{annotation_id}",
            {"question": question, "answer": answer},
        ).json()
        return _annotation(payload)

    def delete(self, annotation: Annotation | str) -> None:
        """Remove an annotation."""
        annotation_id = annotation if isinstance(annotation, str) else annotation.id
        self._client._send_request("DELETE", f"/apps/annotations/{annotation_id}")

    def set_reply(
        self,
        enabled: bool,
        *,
        embedding_model: str | None = None,
        embedding_provider: str | None = None,
        score_threshold: float | None = None,
    ) -> AnnotationReplyJob:
        """Turn annotation reply on or off.

        Enabling it re-embeds every annotation, so Dify answers with a job.
        Poll it with :meth:`reply_status`.
        """
        body: dict[str, Any] = {}
        if embedding_model is not None:
            body["embedding_model_name"] = embedding_model
        if embedding_provider is not None:
            body["embedding_provider_name"] = embedding_provider
        if score_threshold is not None:
            body["score_threshold"] = score_threshold
        action = "enable" if enabled else "disable"
        payload = self._client._send_request(
            "POST", f"/apps/annotation-reply/{action}", body
        ).json()
        return AnnotationReplyJob(
            id=str(payload.get("job_id") or payload.get("id") or ""),
            status=str(payload.get("job_status") or payload.get("status") or ""),
            error=str(payload.get("error_msg") or ""),
        )

    def reply_status(
        self, job: AnnotationReplyJob | str, *, enabled: bool = True
    ) -> AnnotationReplyJob:
        """Ask how the enable or disable job is going."""
        job_id = job if isinstance(job, str) else job.id
        action: Literal["enable", "disable"] = "enable" if enabled else "disable"
        payload = self._client._send_request(
            "GET", f"/apps/annotation-reply/{action}/status/{job_id}"
        ).json()
        return AnnotationReplyJob(
            id=job_id,
            status=str(payload.get("job_status") or payload.get("status") or ""),
            error=str(payload.get("error_msg") or ""),
        )


class AsyncAnnotations(Resource):
    """The async counterpart of :class:`Annotations`."""

    async def list(
        self, *, page: int = 1, limit: int = 20, keyword: str | None = None
    ) -> AsyncPage[Annotation]:
        """List the app's annotations. ``page.all()`` walks every page."""

        async def fetch(number: int) -> dict[str, Any]:
            params: dict[str, Any] = {"page": number, "limit": limit}
            if keyword is not None:
                params["keyword"] = keyword
            response = await self._client._send_request(
                "GET", "/apps/annotations", params=params
            )
            return dict(response.json())

        return by_page_async(await fetch(page), _annotation, fetch, page)

    async def create(self, question: str, answer: str) -> Annotation:
        """Add an annotation."""
        payload = (
            await self._client._send_request(
                "POST", "/apps/annotations", {"question": question, "answer": answer}
            )
        ).json()
        return _annotation(payload)

    async def update(
        self, annotation: Annotation | str, question: str, answer: str
    ) -> Annotation:
        """Replace an annotation's question and answer."""
        annotation_id = annotation if isinstance(annotation, str) else annotation.id
        payload = (
            await self._client._send_request(
                "PUT",
                f"/apps/annotations/{annotation_id}",
                {"question": question, "answer": answer},
            )
        ).json()
        return _annotation(payload)

    async def delete(self, annotation: Annotation | str) -> None:
        """Remove an annotation."""
        annotation_id = annotation if isinstance(annotation, str) else annotation.id
        await self._client._send_request("DELETE", f"/apps/annotations/{annotation_id}")

    async def set_reply(
        self,
        enabled: bool,
        *,
        embedding_model: str | None = None,
        embedding_provider: str | None = None,
        score_threshold: float | None = None,
    ) -> AnnotationReplyJob:
        """Turn annotation reply on or off.

        Enabling it re-embeds every annotation, so Dify answers with a job.
        Poll it with :meth:`reply_status`.
        """
        body: dict[str, Any] = {}
        if embedding_model is not None:
            body["embedding_model_name"] = embedding_model
        if embedding_provider is not None:
            body["embedding_provider_name"] = embedding_provider
        if score_threshold is not None:
            body["score_threshold"] = score_threshold
        action = "enable" if enabled else "disable"
        payload = (
            await self._client._send_request(
                "POST", f"/apps/annotation-reply/{action}", body
            )
        ).json()
        return AnnotationReplyJob(
            id=str(payload.get("job_id") or payload.get("id") or ""),
            status=str(payload.get("job_status") or payload.get("status") or ""),
            error=str(payload.get("error_msg") or ""),
        )

    async def reply_status(
        self, job: AnnotationReplyJob | str, *, enabled: bool = True
    ) -> AnnotationReplyJob:
        """Ask how the enable or disable job is going."""
        job_id = job if isinstance(job, str) else job.id
        action: Literal["enable", "disable"] = "enable" if enabled else "disable"
        payload = (
            await self._client._send_request(
                "GET", f"/apps/annotation-reply/{action}/status/{job_id}"
            )
        ).json()
        return AnnotationReplyJob(
            id=job_id,
            status=str(payload.get("job_status") or payload.get("status") or ""),
            error=str(payload.get("error_msg") or ""),
        )
