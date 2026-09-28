"""The workspace's knowledge bases, addressed by a dataset API key.

A dataset key is not an app key: it scopes to the workspace's datasets and can
do nothing to an app. The entry points follow that::

    knowledge = DifyKnowledge(api_key="dataset-…")

    dataset = knowledge.datasets.create("handbook")
    docs = knowledge.documents(dataset)
    doc = docs.create(text="…", name="policy")
    docs.wait_until_indexed(doc)

    for hit in knowledge.datasets.search(dataset, "what is the refund window?"):
        print(hit.score, hit.segment.content)
"""

from __future__ import annotations

from typing import Any

import httpx

from ._transport import Transport
from .catalog import ModelProvider, providers_from
from .resources.knowledge import (
    AsyncDatasets,
    AsyncDocuments,
    AsyncPipeline,
    AsyncSegments,
    AsyncTags,
    Dataset,
    Datasets,
    Documents,
    Pipeline,
    Segments,
    Tags,
)
from .secrets import ApiKeyInput

__all__ = ["AsyncDifyKnowledge", "DifyKnowledge"]


class DifyKnowledge(Transport):
    """Knowledge bases, their documents, and retrieval against them.

    Args:
        api_key: A dataset API key. Left out, read from ``DIFY_API_KEY``.
        base_url: The Service API root. Left out, derived from ``DIFY_HOST``.
        kwargs: ``timeout``, ``max_retries``, ``retry_delay``,
            ``enable_logging``, ``http_client``.
    """

    def __init__(
        self,
        api_key: ApiKeyInput = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(api_key, base_url=base_url, **kwargs)
        #: The workspace's knowledge bases.
        self.datasets = Datasets(self)
        #: Tags, which are workspace-level rather than per-base.
        self.tags = Tags(self)

    def models(self, model_type: str = "llm") -> list[ModelProvider]:
        """Models of one type the workspace has configured.

        Here rather than on :class:`~dify_client.DifyApp` because Dify guards
        this route with the *dataset* token: an app key answers "Access token
        is invalid", which reads like a broken key rather than like the wrong
        client. It is mostly asked for ``text-embedding`` and ``rerank``, which
        is what configuring a knowledge base needs.
        """
        payload = self._send_request(
            "GET", f"/workspaces/current/models/model-types/{model_type}"
        ).json()
        return providers_from(payload.get("data", []))

    def documents(self, dataset: Dataset | str) -> Documents:
        """The documents of one knowledge base."""
        return Documents(self, dataset)

    def segments(self, dataset: Dataset | str, document: Any) -> Segments:
        """The segments of one document."""
        return Segments(self, dataset, document)

    def pipeline(self, dataset: Dataset | str) -> Pipeline:
        """One knowledge base's RAG pipeline."""
        return Pipeline(self, dataset)

    def upload_for_pipeline(self, file: Any, *, filename: str | None = None) -> Any:
        """Upload a file for a pipeline to consume. Workspace-level."""
        return Pipeline.upload(self, file, filename=filename)

    def __repr__(self) -> str:
        from .secrets import mask_secret

        return (
            f"DifyKnowledge(base_url={self.base_url!r}, "
            f"api_key={mask_secret(self.api_key)!r})"
        )


class AsyncDifyKnowledge:
    """The async counterpart of :class:`DifyKnowledge`.

    The same verbs, awaited::

        async with AsyncDifyKnowledge(api_key="dataset-…") as knowledge:
            dataset = await knowledge.datasets.create("handbook")
            docs = knowledge.documents(dataset)
            doc = await docs.create(text="…", name="policy")
            await docs.wait_until_indexed(doc)

    The calls that only pick out a sub-resource — ``documents``, ``segments``,
    ``pipeline`` — send nothing, so they are not awaited.
    """

    def __init__(
        self,
        api_key: ApiKeyInput = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        from ._async_transport import AsyncTransport

        self._inner = AsyncTransport(api_key, base_url=base_url, **kwargs)
        #: The workspace's knowledge bases.
        self.datasets = AsyncDatasets(self._inner)
        #: Tags, which are workspace-level rather than per-base.
        self.tags = AsyncTags(self._inner)

    @property
    def base_url(self) -> str:
        return self._inner.base_url

    def with_timeout(self, seconds: float | httpx.Timeout | None) -> Any:
        """Give every call inside the block a different timeout.

        Not awaited: entering the block sends nothing.
        """
        return self._inner.with_timeout(seconds)

    async def __aenter__(self) -> AsyncDifyKnowledge:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def models(self, model_type: str = "llm") -> list[ModelProvider]:
        """Models of one type the workspace has configured."""
        response = await self._inner._send_request(
            "GET", f"/workspaces/current/models/model-types/{model_type}"
        )
        return providers_from(response.json().get("data", []))

    def documents(self, dataset: Dataset | str) -> AsyncDocuments:
        """The documents of one knowledge base."""
        return AsyncDocuments(self._inner, dataset)

    def segments(self, dataset: Dataset | str, document: Any) -> AsyncSegments:
        """The segments of one document."""
        return AsyncSegments(self._inner, dataset, document)

    def pipeline(self, dataset: Dataset | str) -> AsyncPipeline:
        """One knowledge base's RAG pipeline."""
        return AsyncPipeline(self._inner, dataset)

    async def upload_for_pipeline(
        self, file: Any, *, filename: str | None = None
    ) -> Any:
        """Upload a file for a pipeline to consume. Workspace-level."""
        return await AsyncPipeline.upload(self._inner, file, filename=filename)

    def __repr__(self) -> str:
        from .secrets import mask_secret

        return (
            f"AsyncDifyKnowledge(base_url={self.base_url!r}, "
            f"api_key={mask_secret(self._inner.api_key)!r})"
        )
