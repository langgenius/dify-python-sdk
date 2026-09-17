"""Knowledge: datasets, the documents in them, and what retrieval finds.

A dataset API key scopes to the workspace's datasets, not to one app, which is
why this hangs off :class:`~dify_client.DifyKnowledge` rather than
:class:`~dify_client.DifyApp`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, List, Literal, NoReturn

from ..exceptions import APIError, RequestTimeout, ValidationError
from ..paging import by_page, by_page_async, unpaged, unpaged_async
from ..results import AsyncPage, Page, WorkflowRun
from ..streams import AsyncWorkflowRunStream, WorkflowRunStream
from ._base import Resource, Transport
from .runs import _run_from_blocking

__all__ = [
    "AsyncDatasets",
    "AsyncDocuments",
    "AsyncPipeline",
    "AsyncSegments",
    "AsyncTags",
    "Dataset",
    "Datasets",
    "Document",
    "Documents",
    "IndexingStatus",
    "Pipeline",
    "PipelineIngestion",
    "RetrievalHit",
    "Segment",
    "MetadataField",
    "Segments",
    "Tag",
    "Tags",
]


@dataclass(frozen=True)
class Dataset:
    """A knowledge base."""

    id: str
    name: str = ""
    description: str = ""
    permission: str = ""
    indexing_technique: str = ""
    document_count: int = 0
    word_count: int = 0
    app_count: int = 0
    payload: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Document:
    """One document inside a dataset."""

    id: str
    name: str = ""
    indexing_status: str = ""
    word_count: int = 0
    enabled: bool = True
    error: str = ""
    batch: str = ""
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def indexed(self) -> bool:
        return self.indexing_status == "completed"


@dataclass(frozen=True)
class IndexingStatus:
    """How far the indexing of one batch has got.

    Uploading a document returns before it is searchable. This is what says
    when it is.
    """

    id: str
    status: str = ""
    completed_segments: int = 0
    total_segments: int = 0
    error: str = ""

    @property
    def finished(self) -> bool:
        return self.status in {"completed", "error", "paused"}

    @property
    def indexed(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True)
class Segment:
    """A chunk of a document, as retrieval sees it."""

    id: str
    content: str = ""
    answer: str = ""
    keywords: tuple[str, ...] = ()
    position: int = 0
    word_count: int = 0
    enabled: bool = True
    payload: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class RetrievalHit:
    """One segment retrieval found, and how well it matched."""

    score: float
    segment: Segment
    document_id: str = ""
    document_name: str = ""

    def __str__(self) -> str:
        return self.segment.content


@dataclass(frozen=True)
class Tag:
    """A label across the workspace's knowledge bases."""

    id: str
    name: str = ""
    type: str = ""
    #: How many knowledge bases carry it. Dify sends this as a *string*, so
    #: `tag["binding_count"] > 0` on the raw payload compared a str to an int.
    binding_count: int = 0
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class MetadataField:
    """A field documents in one knowledge base may carry.

    Two kinds arrive in this shape and the difference is the ``id``: a field
    you defined has one and can be renamed or deleted by it, while one of
    Dify's own — filename, upload date — has none, because those are turned on
    and off rather than managed.
    """

    name: str
    type: str = ""
    id: str = ""
    #: How many documents have a value for it. Absent on Dify's own fields.
    count: int = 0
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def built_in(self) -> bool:
        """Whether Dify fills this one in rather than you."""
        return not self.id

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class PipelineIngestion:
    """What running a published pipeline queued.

    A published run does not answer with the work: it enqueues one document
    per source and answers with the batch they share. The documents are not
    indexed yet — ``documents.indexing_status(ingestion.batch)`` is how far it
    has got, and :meth:`Documents.wait_until_indexed` waits for it.
    """

    batch: str
    documents: list[Document] = field(default_factory=list)
    dataset_id: str = ""
    #: Everything Dify sent, so a field this SDK does not name is not lost.
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def __iter__(self) -> Any:
        return iter(self.documents)

    def __len__(self) -> int:
        return len(self.documents)


def _ingestion(payload: Mapping[str, Any], dataset_id: str) -> PipelineIngestion:
    """Shape a published pipeline run: a batch, and the documents it queued."""
    batch = str(payload.get("batch") or "")
    dataset = payload.get("dataset")
    documents = payload.get("documents")
    return PipelineIngestion(
        batch=batch,
        # The batch lives beside the documents rather than inside them, the
        # same way create() reports it — carried across so each document can
        # be asked about its own indexing.
        documents=[
            _document({**document, "batch": batch})
            for document in (documents if isinstance(documents, list) else [])
        ],
        dataset_id=str((dataset or {}).get("id") or dataset_id),
        payload=dict(payload),
    )


def _dataset(payload: Mapping[str, Any]) -> Dataset:
    return Dataset(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        permission=str(payload.get("permission") or ""),
        indexing_technique=str(payload.get("indexing_technique") or ""),
        document_count=int(payload.get("document_count") or 0),
        word_count=int(payload.get("word_count") or 0),
        app_count=int(payload.get("app_count") or 0),
        payload=dict(payload),
    )


def _document(payload: Mapping[str, Any]) -> Document:
    return Document(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        indexing_status=str(payload.get("indexing_status") or ""),
        word_count=int(payload.get("word_count") or 0),
        enabled=bool(payload.get("enabled", True)),
        error=str(payload.get("error") or ""),
        batch=str(payload.get("batch") or ""),
        payload=dict(payload),
    )


def _created_document(payload: Mapping[str, Any]) -> Document:
    """A document as create and update report it.

    Dify nests the document under ``document`` and puts the indexing batch
    beside it, not inside it — so the batch has to be carried across or the
    document cannot be asked about its own indexing.
    """
    document = payload.get("document")
    document = document if isinstance(document, dict) else dict(payload)
    batch = payload.get("batch") or document.get("batch") or ""
    return _document({**document, "batch": batch})


def _segment(payload: Mapping[str, Any]) -> Segment:
    keywords = payload.get("keywords") or []
    return Segment(
        id=str(payload.get("id") or ""),
        content=str(payload.get("content") or ""),
        answer=str(payload.get("answer") or ""),
        keywords=tuple(str(k) for k in keywords),
        position=int(payload.get("position") or 0),
        word_count=int(payload.get("word_count") or 0),
        enabled=bool(payload.get("enabled", True)),
        payload=dict(payload),
    )


def _id(value: Any) -> str:
    return value if isinstance(value, str) else str(getattr(value, "id", value))


def _tag(payload: Mapping[str, Any]) -> Tag:
    count = payload.get("binding_count")
    return Tag(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        type=str(payload.get("type") or ""),
        # A string in the listing, an int nowhere. Converted here rather than
        # left for every caller to notice.
        binding_count=int(str(count)) if str(count or "").isdigit() else 0,
        payload=dict(payload),
    )


def _metadata_field(payload: Mapping[str, Any]) -> MetadataField:
    return MetadataField(
        name=str(payload.get("name") or ""),
        type=str(payload.get("type") or ""),
        id=str(payload.get("id") or ""),
        count=int(payload.get("count") or 0),
        payload=dict(payload),
    )


def _hits(payload: Mapping[str, Any]) -> List[RetrievalHit]:
    """Shape a retrieval answer. Shared, so sync and async cannot drift."""
    records = payload.get("records") or payload.get("query", {}).get("records") or []
    hits = []
    for record in records:
        segment = record.get("segment") or {}
        document = segment.get("document") or {}
        hits.append(
            RetrievalHit(
                score=float(record.get("score") or 0.0),
                segment=_segment(segment),
                document_id=str(document.get("id") or ""),
                document_name=str(document.get("name") or ""),
            )
        )
    return hits


def _status(payload: Mapping[str, Any]) -> IndexingStatus:
    """Shape an indexing-status answer, which arrives as a one-item list."""
    data = (payload.get("data") or [{}])[0]
    return IndexingStatus(
        id=str(data.get("id") or ""),
        status=str(data.get("indexing_status") or ""),
        completed_segments=int(data.get("completed_segments") or 0),
        total_segments=int(data.get("total_segments") or 0),
        error=str(data.get("error") or ""),
    )


def _one_source(text: Any, file: Any) -> None:
    """Refuse both a text and a file, or neither. Dify takes one route each."""
    if (text is None) == (file is None):
        msg = "Give exactly one of text=… or file=…."
        raise ValidationError(msg)


def _part(file: str | Path | BinaryIO, filename: str | None) -> tuple[str, bytes]:
    """The name and bytes of a file part, from a path or an open file."""
    path = Path(str(file)) if isinstance(file, (str, Path)) else None
    name = filename or (path.name if path else "document")
    content = path.read_bytes() if path else file.read()  # type: ignore[union-attr]
    return name, content


def _batch_key(batch: Document | str) -> str:
    """The indexing batch to ask about, or say why there is not one."""
    key = batch.batch if isinstance(batch, Document) else str(batch)
    if key:
        return key

    msg = (
        "This document carries no indexing batch, so there is nothing "
        "to ask about. Dify reports one when a document is created or "
        "updated; a document read back from list() does not have it."
    )
    raise ValidationError(msg)


def _indexed_or_raise(settled: IndexingStatus) -> IndexingStatus:
    """Return a finished status, or explain why it is not searchable."""
    if settled.indexed:
        return settled

    detail = f": {settled.error}" if settled.error else ""
    msg = (
        f"Indexing stopped at {settled.status!r} rather than completing"
        f"{detail} ({settled.completed_segments}/{settled.total_segments} "
        "segments). The document is not searchable. "
        "wait_until_settled() returns this state instead of raising."
    )
    raise APIError(msg, 0, {"indexing_status": settled.status})


def _timed_out(status: IndexingStatus, timeout: float) -> Exception:
    return RequestTimeout(
        f"Indexing was still {status.status!r} after {timeout:g}s "
        f"({status.completed_segments}/{status.total_segments} "
        "segments). Poll indexing_status() yourself for a longer wait."
    )


def _chosen(documents: Sequence[Document | str]) -> list[str]:
    """The documents to download, or say that Dify will not guess."""
    chosen = [_id(d) for d in documents]
    if chosen:
        return chosen

    msg = (
        "Name the documents to download. Dify has no "
        "download-everything; list() then pass what you want."
    )
    raise ValidationError(msg)


def _raise_no_pipeline(dataset_id: str, failure: APIError) -> NoReturn:
    """Re-raise, saying a knowledge base has no pipeline rather than "not found".

    Dify answers every pipeline route on an ordinary knowledge base with
    "Pipeline not found", which reads like a bug. It is an absence.
    """
    if "pipeline not found" not in str(failure).lower():
        raise failure
    msg = (
        f"Knowledge base {dataset_id} has no RAG pipeline. One "
        "created with datasets.create() indexes on upload instead; "
        "a pipeline comes from creating the base from a pipeline "
        "template in the console."
    )
    raise APIError(msg, failure.status_code, failure.response) from failure


def _run_body(
    start_node_id: str,
    datasource_type: str,
    datasource_info_list: Sequence[Mapping[str, Any]],
    inputs: Mapping[str, Any] | None,
    *,
    published: bool = True,
    response_mode: Literal["blocking", "streaming"] = "blocking",
) -> dict[str, Any]:
    """The body of a pipeline run, published or draft."""
    return {
        "start_node_id": start_node_id,
        "datasource_type": datasource_type,
        "datasource_info_list": [dict(i) for i in datasource_info_list],
        "inputs": dict(inputs or {}),
        "is_published": published,
        "response_mode": response_mode,
    }


def _named_tags(tags: Sequence[Any]) -> list[str]:
    if not tags:
        msg = "Name at least one tag to unbind."
        raise ValidationError(msg)
    return [_id(tag) for tag in tags]


class Datasets(Resource):
    """The workspace's knowledge bases."""

    def create(
        self,
        name: str,
        *,
        description: str | None = None,
        indexing_technique: Literal["high_quality", "economy"] | None = None,
        permission: str | None = None,
        **extra: Any,
    ) -> Dataset:
        """Create a knowledge base."""
        body: dict[str, Any] = {"name": name, **extra}
        if description is not None:
            body["description"] = description
        if indexing_technique is not None:
            body["indexing_technique"] = indexing_technique
        if permission is not None:
            body["permission"] = permission
        return _dataset(self._client._send_request("POST", "/datasets", body).json())

    def list(
        self,
        *,
        page: int = 1,
        limit: int = 20,
        keyword: str | None = None,
        tag_ids: Sequence[str] | None = None,
        include_all: bool | None = None,
    ) -> Page[Dataset]:
        """List the workspace's knowledge bases."""

        def fetch(number: int):
            params: dict[str, Any] = {"page": number, "limit": limit}
            if keyword is not None:
                params["keyword"] = keyword
            if tag_ids:
                params["tag_ids"] = list(tag_ids)
            if include_all is not None:
                params["include_all"] = include_all
            return self._client._send_request("GET", "/datasets", params=params).json()

        return by_page(fetch(page), _dataset, fetch, page)

    def retrieve(self, dataset: Dataset | str) -> Dataset:
        """Read one back."""
        return _dataset(
            self._client._send_request("GET", f"/datasets/{_id(dataset)}").json()
        )

    def update(self, dataset: Dataset | str, **fields: Any) -> Dataset:
        """Change a knowledge base's settings."""
        return _dataset(
            self._client._send_request(
                "PATCH", f"/datasets/{_id(dataset)}", dict(fields)
            ).json()
        )

    def delete(self, dataset: Dataset | str) -> None:
        """Delete a knowledge base and everything in it."""
        self._client._send_request("DELETE", f"/datasets/{_id(dataset)}")

    def search(
        self,
        dataset: Dataset | str,
        query: str,
        *,
        retrieval_model: Mapping[str, Any] | None = None,
        external_retrieval_model: Mapping[str, Any] | None = None,
    ) -> List[RetrievalHit]:
        """Retrieve against a knowledge base, as a node would.

        The same thing the console calls hit testing: it runs retrieval and
        shows what came back, without an app in the way.
        """
        body: dict[str, Any] = {"query": query}
        if retrieval_model is not None:
            body["retrieval_model"] = dict(retrieval_model)
        if external_retrieval_model is not None:
            body["external_retrieval_model"] = dict(external_retrieval_model)
        return _hits(
            self._client._send_request(
                "POST", f"/datasets/{_id(dataset)}/retrieve", body
            ).json()
        )

    def tags(self, dataset: Dataset | str) -> List[Tag]:
        """The tags bound to one knowledge base."""
        payload = self._client._send_request(
            "GET", f"/datasets/{_id(dataset)}/tags"
        ).json()
        return [_tag(item) for item in payload.get("data", [])]

    def metadata(self, dataset: Dataset | str) -> List[MetadataField]:
        """The metadata fields this knowledge base defines."""
        payload = self._client._send_request(
            "GET", f"/datasets/{_id(dataset)}/metadata"
        ).json()
        return [
            _metadata_field(item)
            for item in payload.get("doc_metadata", payload.get("data", []))
        ]

    def add_metadata_field(
        self, dataset: Dataset | str, name: str, type: str = "string"
    ) -> MetadataField:
        """Define a metadata field documents in this base may carry."""
        return _metadata_field(
            self._client._send_request(
                "POST",
                f"/datasets/{_id(dataset)}/metadata",
                {"name": name, "type": type},
            ).json()
        )

    def rename_metadata_field(
        self, dataset: Dataset | str, field: MetadataField | str, name: str
    ) -> MetadataField:
        """Rename a metadata field. The values on documents are kept."""
        return _metadata_field(
            self._client._send_request(
                "PATCH",
                f"/datasets/{_id(dataset)}/metadata/{_id(field)}",
                {"name": name},
            ).json()
        )

    def delete_metadata_field(
        self, dataset: Dataset | str, field: MetadataField | str
    ) -> None:
        """Remove a metadata field, and its values from every document."""
        self._client._send_request(
            "DELETE", f"/datasets/{_id(dataset)}/metadata/{_id(field)}"
        )

    def built_in_metadata(self, dataset: Dataset | str) -> List[MetadataField]:
        """The fields Dify maintains itself — filename, upload date and such.

        Separate from the fields you define: these are filled in for you, and
        are turned on or off rather than created. They carry no id, which is
        what ``field.built_in`` reads.
        """
        payload = self._client._send_request(
            "GET", f"/datasets/{_id(dataset)}/metadata/built-in"
        ).json()
        return [
            _metadata_field(item)
            for item in payload.get("fields", payload.get("data", []))
        ]

    def set_built_in_metadata(self, dataset: Dataset | str, enabled: bool) -> None:
        """Turn Dify's own metadata fields on or off for this knowledge base."""
        action = "enable" if enabled else "disable"
        self._client._send_request(
            "POST", f"/datasets/{_id(dataset)}/metadata/built-in/{action}"
        )


class Documents(Resource):
    """The documents inside one knowledge base.

    Bound to a dataset, so the id is not repeated on every call::

        docs = knowledge.documents(dataset)
        doc = docs.create(text="…", name="notes")
        docs.wait_until_indexed(doc)
    """

    def __init__(self, client: Transport, dataset: Dataset | str) -> None:
        super().__init__(client)
        self.dataset_id = _id(dataset)

    def _path(self, *parts: str) -> str:
        return "/".join(("/datasets", self.dataset_id, *parts))

    def create(
        self,
        *,
        text: str | None = None,
        file: str | Path | BinaryIO | None = None,
        name: str | None = None,
        filename: str | None = None,
        indexing_technique: str = "high_quality",
        process_rule: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> Document:
        """Add a document, from text or from a file.

        Uses Dify's hyphenated routes (``create-by-text``, ``create-by-file``).
        The underscored spellings are the same operation and are marked
        deprecated.

        Returns before indexing finishes; the document is not searchable until
        it does. :meth:`wait_until_indexed` waits, :meth:`indexing_status` asks.
        """
        _one_source(text, file)

        settings: dict[str, Any] = {
            "indexing_technique": indexing_technique,
            "process_rule": dict(process_rule or {"mode": "automatic"}),
            **extra,
        }
        if text is not None:
            settings.update({"name": name or "document", "text": text})
            payload = self._client._send_request(
                "POST", self._path("document", "create-by-text"), settings
            ).json()
        else:
            import json as _json

            part_name, content = _part(file, filename)  # type: ignore[arg-type]
            payload = self._client._send_request_with_files(
                "POST",
                self._path("document", "create-by-file"),
                data={"data": _json.dumps(settings)},
                files={"file": (part_name, content)},
            ).json()

        return _created_document(payload)

    def list(
        self,
        *,
        page: int | None = None,
        limit: int | None = None,
        keyword: str | None = None,
        status: str | None = None,
    ) -> Page[Document]:
        """List this knowledge base's documents."""

        def fetch(number: int):
            return self._client._send_request(
                "GET",
                self._path("documents"),
                params={
                    "page": number,
                    "limit": limit,
                    "keyword": keyword,
                    "status": status,
                },
            ).json()

        start = page or 1
        return by_page(fetch(start), _document, fetch, start)

    def update(
        self,
        document: Document | str,
        *,
        text: str | None = None,
        file: str | Path | BinaryIO | None = None,
        name: str | None = None,
        filename: str | None = None,
        **extra: Any,
    ) -> Document:
        """Replace a document's contents, from text or from a file."""
        _one_source(text, file)

        document_id = _id(document)
        if text is not None:
            # Dify requires a name alongside text, and answers a pydantic
            # validation error rather than saying which field it wants. Keep
            # the document's own name when the caller does not give one.
            if name is None:
                name = getattr(document, "name", "") or self.retrieve(document).name
            body: dict[str, Any] = {"text": text, "name": name, **extra}
            payload = self._client._send_request(
                "POST",
                self._path("documents", document_id, "update-by-text"),
                body,
            ).json()
        else:
            import json as _json

            part_name, content = _part(file, filename)  # type: ignore[arg-type]
            # PATCH on the document itself. Dify marks *both* spellings of
            # `/update-by-file` deprecated; this is the one route that is not.
            payload = self._client._send_request_with_files(
                "PATCH",
                self._path("documents", document_id),
                data={"data": _json.dumps(dict(extra))},
                files={"file": (part_name, content)},
            ).json()
        # The same conversion as create(): the batch an update belongs to sits
        # outside the document, and dropping it made indexing_status() ask
        # about /documents//indexing-status.
        return _created_document(payload)

    def retrieve(self, document: Document | str) -> Document:
        """Read one document back, with its current indexing state."""
        payload = self._client._send_request(
            "GET", self._path("documents", _id(document))
        ).json()
        return _document(payload.get("data") or payload.get("document") or payload)

    def delete(self, document: Document | str) -> None:
        """Remove a document and its segments."""
        self._client._send_request("DELETE", self._path("documents", _id(document)))

    def indexing_status(self, batch: Document | str) -> IndexingStatus:
        """How far indexing has got, for the batch a document arrived in."""
        key = _batch_key(batch)
        return _status(
            self._client._send_request(
                "GET", self._path("documents", key, "indexing-status")
            ).json()
        )

    def wait_until_settled(
        self, document: Document | str, *, timeout: float = 120.0, poll: float = 1.0
    ) -> IndexingStatus:
        """Block until indexing stops, however it stops.

        Returns the state it stopped in — including ``error`` and ``paused``.
        Use this when the outcome is what you want to inspect;
        :meth:`wait_until_indexed` when you want to carry on only if it worked.
        """
        import time

        deadline = time.monotonic() + timeout
        while True:
            status = self.indexing_status(document)
            if status.finished:
                return status
            if time.monotonic() >= deadline:
                raise _timed_out(status, timeout)
            time.sleep(poll)

    def wait_until_indexed(
        self, document: Document | str, *, timeout: float = 120.0, poll: float = 1.0
    ) -> IndexingStatus:
        """Block until the document is **searchable**, or raise.

        A document is not retrievable the moment it uploads, which is the usual
        surprise when a freshly added one returns no hits.

        Indexing that stopped without finishing — ``error``, or ``paused``
        because the workspace ran out of quota — raises rather than returning.
        It used to return, so a caller went on to search a knowledge base that
        had silently indexed nothing.
        """
        settled = self.wait_until_settled(document, timeout=timeout, poll=poll)
        return _indexed_or_raise(settled)

    def set_enabled(self, documents: Sequence[Document | str], enabled: bool) -> None:
        """Turn documents on or off for retrieval, without deleting them."""
        action = "enable" if enabled else "disable"
        self._client._send_request(
            "PATCH",
            self._path("documents", "status", action),
            {"document_ids": [_id(d) for d in documents]},
        )

    def download(self, document: Document | str) -> bytes:
        """The document's original file."""
        return self._client._send_request(
            "GET", self._path("documents", _id(document), "download")
        ).content

    def download_all(self, documents: Sequence[Document | str]) -> bytes:
        """Several documents at once, as a zip.

        A POST, not a GET, and the ids are required rather than meaning "all":
        a knowledge base can hold more of them than a URL would carry, and Dify
        will not guess at which ones you meant.
        """
        chosen = _chosen(documents)
        return self._client._send_request(
            "POST", self._path("documents", "download-zip"), {"document_ids": chosen}
        ).content

    def set_metadata(self, operations: Sequence[Mapping[str, Any]]) -> None:
        """Write metadata onto documents in bulk."""
        self._client._send_request(
            "POST",
            self._path("documents", "metadata"),
            {"operation_data": [dict(op) for op in operations]},
        )

    def segments(self, document: Document | str) -> Segments:
        """The chunks of one document."""
        return Segments(self._client, self.dataset_id, document)


class Segments(Resource):
    """The chunks of one document, and the child chunks beneath them."""

    def __init__(
        self, client: Transport, dataset: Dataset | str, document: Document | str
    ):
        super().__init__(client)
        self.dataset_id = _id(dataset)
        self.document_id = _id(document)

    def _path(self, *parts: str) -> str:
        return "/".join(
            ("/datasets", self.dataset_id, "documents", self.document_id, *parts)
        )

    def list(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int | None = None,
        limit: int | None = None,
    ) -> Page[Segment]:
        """List this document's segments."""

        def fetch(number: int):
            return self._client._send_request(
                "GET",
                self._path("segments"),
                params={
                    "keyword": keyword,
                    "status": status,
                    "page": number,
                    "limit": limit,
                },
            ).json()

        start = page or 1
        return by_page(fetch(start), _segment, fetch, start)

    def retrieve(self, segment: Segment | str) -> Segment:
        """Read one segment back."""
        payload = self._client._send_request(
            "GET", self._path("segments", _id(segment))
        ).json()
        return _segment(payload.get("data") or payload)

    def create(self, segments: Sequence[Mapping[str, Any]]) -> List[Segment]:
        """Add segments by hand, rather than letting Dify chunk."""
        payload = self._client._send_request(
            "POST", self._path("segments"), {"segments": [dict(s) for s in segments]}
        ).json()
        return [_segment(item) for item in payload.get("data", [])]

    def update(self, segment: Segment | str, **fields: Any) -> Segment:
        """Change one segment."""
        payload = self._client._send_request(
            "POST",
            self._path("segments", _id(segment)),
            {"segment": dict(fields)},
        ).json()
        return _segment(payload.get("data") or payload)

    def delete(self, segment: Segment | str) -> None:
        """Remove one segment."""
        self._client._send_request("DELETE", self._path("segments", _id(segment)))

    def child_chunks(
        self,
        segment: Segment | str,
        *,
        keyword: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> List[dict[str, Any]]:
        """The child chunks of one segment, for parent-child indexing."""
        params: dict[str, Any] = {"page": page, "limit": limit}
        if keyword is not None:
            params["keyword"] = keyword
        payload = self._client._send_request(
            "GET", self._path("segments", _id(segment), "child_chunks"), params=params
        ).json()
        return list(payload.get("data", []))

    def add_child_chunk(self, segment: Segment | str, content: str) -> dict[str, Any]:
        """Add one child chunk."""
        return self._client._send_request(
            "POST",
            self._path("segments", _id(segment), "child_chunks"),
            {"content": content},
        ).json()

    def update_child_chunk(
        self, segment: Segment | str, chunk_id: str, content: str
    ) -> dict[str, Any]:
        """Change one child chunk."""
        return self._client._send_request(
            "PATCH",
            self._path("segments", _id(segment), "child_chunks", chunk_id),
            {"content": content},
        ).json()

    def delete_child_chunk(self, segment: Segment | str, chunk_id: str) -> None:
        """Remove one child chunk."""
        self._client._send_request(
            "DELETE", self._path("segments", _id(segment), "child_chunks", chunk_id)
        )


class Tags(Resource):
    """Tags across the workspace's knowledge bases.

    Workspace-level, not per-dataset: a tag exists once and is bound to as many
    knowledge bases as you like. ``Datasets.tags(dataset)`` reads the other
    direction — which tags one base carries.
    """

    def list(self) -> Page[Tag]:
        """Every tag in the workspace.

        Dify answers this one with a bare array and no paging at all, so the
        page holds the lot — still a page, so a caller need not know which
        listings page and which do not.
        """
        payload = self._client._send_request("GET", "/datasets/tags").json()
        items = payload if isinstance(payload, list) else payload.get("data", [])
        return unpaged([_tag(item) for item in items])

    def create(self, name: str) -> Tag:
        """Add a tag."""
        return _tag(
            self._client._send_request(
                "POST", "/datasets/tags", {"name": name, "type": "knowledge"}
            ).json()
        )

    def rename(self, tag: Tag | str, name: str) -> Tag:
        """Change a tag's name. Its bindings are kept."""
        return _tag(
            self._client._send_request(
                "PATCH", "/datasets/tags", {"tag_id": _id(tag), "name": name}
            ).json()
        )

    def delete(self, tag: Tag | str) -> None:
        """Remove a tag from the workspace, and from everything it was on."""
        self._client._send_request("DELETE", "/datasets/tags", {"tag_id": _id(tag)})

    def bind(self, dataset: Dataset | str, tags: Sequence[Tag | str]) -> None:
        """Put these tags on a knowledge base."""
        self._client._send_request(
            "POST",
            "/datasets/tags/binding",
            {"tag_ids": [_id(tag) for tag in tags], "target_id": _id(dataset)},
        )

    def unbind(self, dataset: Dataset | str, *tags: Tag | str) -> None:
        """Take tags off a knowledge base. The tags themselves remain.

        Sends ``tag_ids``. Dify still accepts a singular ``tag_id`` and marks it
        deprecated in the payload's own schema — a route being current does not
        make every field on it current.
        """
        self._client._send_request(
            "POST",
            "/datasets/tags/unbinding",
            {"tag_ids": _named_tags(tags), "target_id": _id(dataset)},
        )


class Pipeline(Resource):
    """A knowledge base's RAG pipeline: how documents get in and get indexed.

    A pipeline is a workflow in its own right — datasource nodes that fetch,
    and processing that chunks and embeds. These run it, rather than letting
    Dify run it on upload.

    **Not every knowledge base has one.** A base created with
    ``datasets.create()`` indexes on upload and has no pipeline; Dify answers
    these calls with "Pipeline not found", which reads like a bug rather than
    like an absence. A pipeline comes from creating the base from a pipeline
    template in the console.
    """

    def __init__(self, client: Transport, dataset: Dataset | str) -> None:
        super().__init__(client)
        self.dataset_id = _id(dataset)

    def datasources(self, *, published: bool = True) -> List[dict[str, Any]]:
        """The datasource plugins this pipeline can pull from."""
        payload = self._request(
            "GET",
            f"/datasets/{self.dataset_id}/pipeline/datasource-plugins",
            params={"is_published": published},
        ).json()
        return list(payload.get("data", []))

    def _request(self, method: str, path: str, *args: Any, **kwargs: Any) -> Any:
        """Send, turning "Pipeline not found" into something actionable."""
        try:
            return self._client._send_request(method, path, *args, **kwargs)
        except APIError as failure:
            _raise_no_pipeline(self.dataset_id, failure)

    def run_datasource(
        self,
        node_id: str,
        inputs: Mapping[str, Any] | None = None,
        *,
        datasource_type: str,
        credential_id: str | None = None,
        published: bool = True,
    ) -> dict[str, Any]:
        """Run one datasource node — fetch, without indexing what it found."""
        body: dict[str, Any] = {
            "inputs": dict(inputs or {}),
            "datasource_type": datasource_type,
            "is_published": published,
        }
        if credential_id:
            body["credential_id"] = credential_id
        return self._request(
            "POST",
            f"/datasets/{self.dataset_id}/pipeline/datasource/nodes/{node_id}/run",
            body,
        ).json()

    def run(
        self,
        *,
        start_node_id: str,
        datasource_type: str,
        datasource_info_list: Sequence[Mapping[str, Any]],
        inputs: Mapping[str, Any] | None = None,
    ) -> PipelineIngestion:
        """Run the published pipeline over the given sources.

        Queued, not awaited: Dify enqueues one document per source and answers
        straight away with the batch they share. Nothing is indexed yet, so
        there is no run to watch and no stream to read — wait on the documents
        instead::

            queued = knowledge.pipeline(dataset).run(...)
            docs = knowledge.documents(dataset)
            docs.wait_until_indexed(queued.batch)

        :meth:`run_draft` is the other thing this route does: running the
        *unpublished* graph, which is a workflow run and reports like one.
        """
        response = self._request(
            "POST",
            f"/datasets/{self.dataset_id}/pipeline/run",
            _run_body(start_node_id, datasource_type, datasource_info_list, inputs),
        )
        return _ingestion(response.json(), self.dataset_id)

    def run_draft(
        self,
        *,
        start_node_id: str,
        datasource_type: str,
        datasource_info_list: Sequence[Mapping[str, Any]],
        inputs: Mapping[str, Any] | None = None,
    ) -> WorkflowRun:
        """Run the *draft* pipeline and wait for it, as the console does.

        A draft run indexes nothing and queues nothing: it executes the graph
        so you can see what it would do. Dify runs it as a workflow and reports
        it as one, which is why this returns the same
        :class:`~dify_client.results.WorkflowRun` as ``app.workflows.runs``
        rather than a type of its own.
        """
        response = self._request(
            "POST",
            f"/datasets/{self.dataset_id}/pipeline/run",
            _run_body(
                start_node_id,
                datasource_type,
                datasource_info_list,
                inputs,
                published=False,
            ),
        )
        return _run_from_blocking(response.json())

    def stream_draft(
        self,
        *,
        start_node_id: str,
        datasource_type: str,
        datasource_info_list: Sequence[Mapping[str, Any]],
        inputs: Mapping[str, Any] | None = None,
        raise_on_error: bool = True,
    ) -> WorkflowRunStream:
        """Run the draft pipeline and watch it happen.

        The events are workflow events — Dify sends the pipeline's run through
        the same stream — so this is a
        :class:`~dify_client.streams.WorkflowRunStream`, iterated and closed
        like any other. Streaming a *published* run is not a thing: that one is
        queued, and :meth:`run` returns as soon as it is.
        """
        response = self._request(
            "POST",
            f"/datasets/{self.dataset_id}/pipeline/run",
            _run_body(
                start_node_id,
                datasource_type,
                datasource_info_list,
                inputs,
                published=False,
                response_mode="streaming",
            ),
            stream=True,
        )
        return WorkflowRunStream(response, raise_on_error=raise_on_error)

    @staticmethod
    def upload(
        client: Transport, file: str | Path | BinaryIO, *, filename: str | None = None
    ) -> dict[str, Any]:
        """Upload a file for a pipeline to pick up.

        Workspace-level rather than per-pipeline, which is why it is a
        ``staticmethod`` here and exposed as ``knowledge.upload_for_pipeline``.
        """
        name, content = _part(file, filename or "upload")
        return client._send_request_with_files(
            "POST",
            "/datasets/pipeline/file-upload",
            data={},
            files={"file": (name, content)},
        ).json()


class AsyncDatasets(Resource):
    """The async counterpart of :class:`Datasets`."""

    async def create(
        self,
        name: str,
        *,
        description: str | None = None,
        indexing_technique: Literal["high_quality", "economy"] | None = None,
        permission: str | None = None,
        **extra: Any,
    ) -> Dataset:
        """Create a knowledge base."""
        body: dict[str, Any] = {"name": name, **extra}
        if description is not None:
            body["description"] = description
        if indexing_technique is not None:
            body["indexing_technique"] = indexing_technique
        if permission is not None:
            body["permission"] = permission
        response = await self._client._send_request("POST", "/datasets", body)
        return _dataset(response.json())

    async def list(
        self,
        *,
        page: int = 1,
        limit: int = 20,
        keyword: str | None = None,
        tag_ids: Sequence[str] | None = None,
        include_all: bool | None = None,
    ) -> AsyncPage[Dataset]:
        """List the workspace's knowledge bases."""

        async def fetch(number: int) -> dict[str, Any]:
            params: dict[str, Any] = {"page": number, "limit": limit}
            if keyword is not None:
                params["keyword"] = keyword
            if tag_ids:
                params["tag_ids"] = list(tag_ids)
            if include_all is not None:
                params["include_all"] = include_all
            response = await self._client._send_request(
                "GET", "/datasets", params=params
            )
            return dict(response.json())

        return by_page_async(await fetch(page), _dataset, fetch, page)

    async def retrieve(self, dataset: Dataset | str) -> Dataset:
        """Read one back."""
        response = await self._client._send_request("GET", f"/datasets/{_id(dataset)}")
        return _dataset(response.json())

    async def update(self, dataset: Dataset | str, **fields: Any) -> Dataset:
        """Change a knowledge base's settings."""
        response = await self._client._send_request(
            "PATCH", f"/datasets/{_id(dataset)}", dict(fields)
        )
        return _dataset(response.json())

    async def delete(self, dataset: Dataset | str) -> None:
        """Delete a knowledge base and everything in it."""
        await self._client._send_request("DELETE", f"/datasets/{_id(dataset)}")

    async def search(
        self,
        dataset: Dataset | str,
        query: str,
        *,
        retrieval_model: Mapping[str, Any] | None = None,
        external_retrieval_model: Mapping[str, Any] | None = None,
    ) -> List[RetrievalHit]:
        """Retrieve against a knowledge base, as a node would."""
        body: dict[str, Any] = {"query": query}
        if retrieval_model is not None:
            body["retrieval_model"] = dict(retrieval_model)
        if external_retrieval_model is not None:
            body["external_retrieval_model"] = dict(external_retrieval_model)
        response = await self._client._send_request(
            "POST", f"/datasets/{_id(dataset)}/retrieve", body
        )
        return _hits(response.json())

    async def tags(self, dataset: Dataset | str) -> List[Tag]:
        """The tags bound to one knowledge base."""
        response = await self._client._send_request(
            "GET", f"/datasets/{_id(dataset)}/tags"
        )
        return [_tag(item) for item in response.json().get("data", [])]

    async def metadata(self, dataset: Dataset | str) -> List[MetadataField]:
        """The metadata fields this knowledge base defines."""
        response = await self._client._send_request(
            "GET", f"/datasets/{_id(dataset)}/metadata"
        )
        payload = response.json()
        return [
            _metadata_field(item)
            for item in payload.get("doc_metadata", payload.get("data", []))
        ]

    async def add_metadata_field(
        self, dataset: Dataset | str, name: str, type: str = "string"
    ) -> MetadataField:
        """Define a metadata field documents in this base may carry."""
        response = await self._client._send_request(
            "POST", f"/datasets/{_id(dataset)}/metadata", {"name": name, "type": type}
        )
        return _metadata_field(response.json())

    async def rename_metadata_field(
        self, dataset: Dataset | str, field: MetadataField | str, name: str
    ) -> MetadataField:
        """Rename a metadata field. The values on documents are kept."""
        response = await self._client._send_request(
            "PATCH", f"/datasets/{_id(dataset)}/metadata/{_id(field)}", {"name": name}
        )
        return _metadata_field(response.json())

    async def delete_metadata_field(
        self, dataset: Dataset | str, field: MetadataField | str
    ) -> None:
        """Remove a metadata field, and its values from every document."""
        await self._client._send_request(
            "DELETE", f"/datasets/{_id(dataset)}/metadata/{_id(field)}"
        )

    async def built_in_metadata(self, dataset: Dataset | str) -> List[MetadataField]:
        """The fields Dify maintains itself — filename, upload date and such."""
        response = await self._client._send_request(
            "GET", f"/datasets/{_id(dataset)}/metadata/built-in"
        )
        payload = response.json()
        return [
            _metadata_field(item)
            for item in payload.get("fields", payload.get("data", []))
        ]

    async def set_built_in_metadata(
        self, dataset: Dataset | str, enabled: bool
    ) -> None:
        """Turn Dify's own metadata fields on or off for this knowledge base."""
        action = "enable" if enabled else "disable"
        await self._client._send_request(
            "POST", f"/datasets/{_id(dataset)}/metadata/built-in/{action}"
        )


class AsyncDocuments(Resource):
    """The async counterpart of :class:`Documents`."""

    def __init__(self, client: Transport, dataset: Dataset | str) -> None:
        super().__init__(client)
        self.dataset_id = _id(dataset)

    def _path(self, *parts: str) -> str:
        return "/".join(("/datasets", self.dataset_id, *parts))

    async def create(
        self,
        *,
        text: str | None = None,
        file: str | Path | BinaryIO | None = None,
        name: str | None = None,
        filename: str | None = None,
        indexing_technique: str = "high_quality",
        process_rule: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> Document:
        """Add a document, from text or from a file."""
        _one_source(text, file)

        settings: dict[str, Any] = {
            "indexing_technique": indexing_technique,
            "process_rule": dict(process_rule or {"mode": "automatic"}),
            **extra,
        }
        if text is not None:
            settings.update({"name": name or "document", "text": text})
            response = await self._client._send_request(
                "POST", self._path("document", "create-by-text"), settings
            )
        else:
            import json as _json

            part_name, content = _part(file, filename)  # type: ignore[arg-type]
            response = await self._client._send_request_with_files(
                "POST",
                self._path("document", "create-by-file"),
                data={"data": _json.dumps(settings)},
                files={"file": (part_name, content)},
            )
        return _created_document(response.json())

    async def list(
        self,
        *,
        page: int | None = None,
        limit: int | None = None,
        keyword: str | None = None,
        status: str | None = None,
    ) -> AsyncPage[Document]:
        """List this knowledge base's documents."""

        async def fetch(number: int) -> dict[str, Any]:
            response = await self._client._send_request(
                "GET",
                self._path("documents"),
                params={
                    "page": number,
                    "limit": limit,
                    "keyword": keyword,
                    "status": status,
                },
            )
            return dict(response.json())

        start = page or 1
        return by_page_async(await fetch(start), _document, fetch, start)

    async def update(
        self,
        document: Document | str,
        *,
        text: str | None = None,
        file: str | Path | BinaryIO | None = None,
        name: str | None = None,
        filename: str | None = None,
        **extra: Any,
    ) -> Document:
        """Replace a document's contents, from text or from a file."""
        _one_source(text, file)

        document_id = _id(document)
        if text is not None:
            if name is None:
                name = (
                    getattr(document, "name", "")
                    or (await self.retrieve(document)).name
                )
            body: dict[str, Any] = {"text": text, "name": name, **extra}
            response = await self._client._send_request(
                "POST", self._path("documents", document_id, "update-by-text"), body
            )
        else:
            import json as _json

            part_name, content = _part(file, filename)  # type: ignore[arg-type]
            response = await self._client._send_request_with_files(
                "PATCH",
                self._path("documents", document_id),
                data={"data": _json.dumps(dict(extra))},
                files={"file": (part_name, content)},
            )
        return _created_document(response.json())

    async def retrieve(self, document: Document | str) -> Document:
        """Read one document back, with its current indexing state."""
        response = await self._client._send_request(
            "GET", self._path("documents", _id(document))
        )
        payload = response.json()
        return _document(payload.get("data") or payload.get("document") or payload)

    async def delete(self, document: Document | str) -> None:
        """Remove a document and its segments."""
        await self._client._send_request(
            "DELETE", self._path("documents", _id(document))
        )

    async def indexing_status(self, batch: Document | str) -> IndexingStatus:
        """How far indexing has got, for the batch a document arrived in."""
        key = _batch_key(batch)
        response = await self._client._send_request(
            "GET", self._path("documents", key, "indexing-status")
        )
        return _status(response.json())

    async def wait_until_settled(
        self, document: Document | str, *, timeout: float = 120.0, poll: float = 1.0
    ) -> IndexingStatus:
        """Block until indexing stops, however it stops.

        Awaited rather than blocking: the wait gives the loop back, so other
        work carries on while Dify indexes.
        """
        import asyncio
        import time

        deadline = time.monotonic() + timeout
        while True:
            status = await self.indexing_status(document)
            if status.finished:
                return status
            if time.monotonic() >= deadline:
                raise _timed_out(status, timeout)
            await asyncio.sleep(poll)

    async def wait_until_indexed(
        self, document: Document | str, *, timeout: float = 120.0, poll: float = 1.0
    ) -> IndexingStatus:
        """Block until the document is **searchable**, or raise."""
        settled = await self.wait_until_settled(document, timeout=timeout, poll=poll)
        return _indexed_or_raise(settled)

    async def set_enabled(
        self, documents: Sequence[Document | str], enabled: bool
    ) -> None:
        """Turn documents on or off for retrieval, without deleting them."""
        action = "enable" if enabled else "disable"
        await self._client._send_request(
            "PATCH",
            self._path("documents", "status", action),
            {"document_ids": [_id(d) for d in documents]},
        )

    async def download(self, document: Document | str) -> bytes:
        """The document's original file."""
        response = await self._client._send_request(
            "GET", self._path("documents", _id(document), "download")
        )
        return bytes(response.content)

    async def download_all(self, documents: Sequence[Document | str]) -> bytes:
        """Several documents at once, as a zip."""
        response = await self._client._send_request(
            "POST",
            self._path("documents", "download-zip"),
            {"document_ids": _chosen(documents)},
        )
        return bytes(response.content)

    async def set_metadata(self, operations: Sequence[Mapping[str, Any]]) -> None:
        """Write metadata onto documents in bulk."""
        await self._client._send_request(
            "POST",
            self._path("documents", "metadata"),
            {"operation_data": [dict(op) for op in operations]},
        )

    def segments(self, document: Document | str) -> AsyncSegments:
        """The chunks of one document. Not a request, so not awaited."""
        return AsyncSegments(self._client, self.dataset_id, document)


class AsyncSegments(Resource):
    """The async counterpart of :class:`Segments`."""

    def __init__(
        self, client: Transport, dataset: Dataset | str, document: Document | str
    ):
        super().__init__(client)
        self.dataset_id = _id(dataset)
        self.document_id = _id(document)

    def _path(self, *parts: str) -> str:
        return "/".join(
            ("/datasets", self.dataset_id, "documents", self.document_id, *parts)
        )

    async def list(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int | None = None,
        limit: int | None = None,
    ) -> AsyncPage[Segment]:
        """List this document's segments."""

        async def fetch(number: int) -> dict[str, Any]:
            response = await self._client._send_request(
                "GET",
                self._path("segments"),
                params={
                    "keyword": keyword,
                    "status": status,
                    "page": number,
                    "limit": limit,
                },
            )
            return dict(response.json())

        start = page or 1
        return by_page_async(await fetch(start), _segment, fetch, start)

    async def retrieve(self, segment: Segment | str) -> Segment:
        """Read one segment back."""
        response = await self._client._send_request(
            "GET", self._path("segments", _id(segment))
        )
        payload = response.json()
        return _segment(payload.get("data") or payload)

    async def create(self, segments: Sequence[Mapping[str, Any]]) -> List[Segment]:
        """Add segments by hand, rather than letting Dify chunk."""
        response = await self._client._send_request(
            "POST", self._path("segments"), {"segments": [dict(s) for s in segments]}
        )
        return [_segment(item) for item in response.json().get("data", [])]

    async def update(self, segment: Segment | str, **fields: Any) -> Segment:
        """Change one segment."""
        response = await self._client._send_request(
            "POST", self._path("segments", _id(segment)), {"segment": dict(fields)}
        )
        payload = response.json()
        return _segment(payload.get("data") or payload)

    async def delete(self, segment: Segment | str) -> None:
        """Remove one segment."""
        await self._client._send_request("DELETE", self._path("segments", _id(segment)))

    async def child_chunks(
        self,
        segment: Segment | str,
        *,
        keyword: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> List[dict[str, Any]]:
        """The child chunks of one segment, for parent-child indexing."""
        params: dict[str, Any] = {"page": page, "limit": limit}
        if keyword is not None:
            params["keyword"] = keyword
        response = await self._client._send_request(
            "GET", self._path("segments", _id(segment), "child_chunks"), params=params
        )
        return list(response.json().get("data", []))

    async def add_child_chunk(
        self, segment: Segment | str, content: str
    ) -> dict[str, Any]:
        """Add one child chunk."""
        response = await self._client._send_request(
            "POST",
            self._path("segments", _id(segment), "child_chunks"),
            {"content": content},
        )
        return dict(response.json())

    async def update_child_chunk(
        self, segment: Segment | str, chunk_id: str, content: str
    ) -> dict[str, Any]:
        """Change one child chunk."""
        response = await self._client._send_request(
            "PATCH",
            self._path("segments", _id(segment), "child_chunks", chunk_id),
            {"content": content},
        )
        return dict(response.json())

    async def delete_child_chunk(self, segment: Segment | str, chunk_id: str) -> None:
        """Remove one child chunk."""
        await self._client._send_request(
            "DELETE", self._path("segments", _id(segment), "child_chunks", chunk_id)
        )


class AsyncTags(Resource):
    """The async counterpart of :class:`Tags`."""

    async def list(self) -> AsyncPage[Tag]:
        """Every tag in the workspace. Dify answers with a bare array."""
        response = await self._client._send_request("GET", "/datasets/tags")
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("data", [])
        return unpaged_async([_tag(item) for item in items])

    async def create(self, name: str) -> Tag:
        """Add a tag."""
        response = await self._client._send_request(
            "POST", "/datasets/tags", {"name": name, "type": "knowledge"}
        )
        return _tag(response.json())

    async def rename(self, tag: Tag | str, name: str) -> Tag:
        """Change a tag's name. Its bindings are kept."""
        response = await self._client._send_request(
            "PATCH", "/datasets/tags", {"tag_id": _id(tag), "name": name}
        )
        return _tag(response.json())

    async def delete(self, tag: Tag | str) -> None:
        """Remove a tag from the workspace, and from everything it was on."""
        await self._client._send_request(
            "DELETE", "/datasets/tags", {"tag_id": _id(tag)}
        )

    async def bind(self, dataset: Dataset | str, tags: Sequence[Tag | str]) -> None:
        """Put these tags on a knowledge base."""
        await self._client._send_request(
            "POST",
            "/datasets/tags/binding",
            {"tag_ids": [_id(tag) for tag in tags], "target_id": _id(dataset)},
        )

    async def unbind(self, dataset: Dataset | str, *tags: Tag | str) -> None:
        """Take tags off a knowledge base. The tags themselves remain."""
        await self._client._send_request(
            "POST",
            "/datasets/tags/unbinding",
            {"tag_ids": _named_tags(tags), "target_id": _id(dataset)},
        )


class AsyncPipeline(Resource):
    """The async counterpart of :class:`Pipeline`."""

    def __init__(self, client: Transport, dataset: Dataset | str) -> None:
        super().__init__(client)
        self.dataset_id = _id(dataset)

    async def _request(self, method: str, path: str, *args: Any, **kwargs: Any) -> Any:
        """Send, turning "Pipeline not found" into something actionable."""
        try:
            return await self._client._send_request(method, path, *args, **kwargs)
        except APIError as failure:
            _raise_no_pipeline(self.dataset_id, failure)

    async def datasources(self, *, published: bool = True) -> List[dict[str, Any]]:
        """The datasource plugins this pipeline can pull from."""
        response = await self._request(
            "GET",
            f"/datasets/{self.dataset_id}/pipeline/datasource-plugins",
            params={"is_published": published},
        )
        return list(response.json().get("data", []))

    async def run_datasource(
        self,
        node_id: str,
        inputs: Mapping[str, Any] | None = None,
        *,
        datasource_type: str,
        credential_id: str | None = None,
        published: bool = True,
    ) -> dict[str, Any]:
        """Run one datasource node — fetch, without indexing what it found."""
        body: dict[str, Any] = {
            "inputs": dict(inputs or {}),
            "datasource_type": datasource_type,
            "is_published": published,
        }
        if credential_id:
            body["credential_id"] = credential_id
        response = await self._request(
            "POST",
            f"/datasets/{self.dataset_id}/pipeline/datasource/nodes/{node_id}/run",
            body,
        )
        return dict(response.json())

    async def run(
        self,
        *,
        start_node_id: str,
        datasource_type: str,
        datasource_info_list: Sequence[Mapping[str, Any]],
        inputs: Mapping[str, Any] | None = None,
    ) -> PipelineIngestion:
        """Run the published pipeline over the given sources. Queued, not awaited."""
        response = await self._request(
            "POST",
            f"/datasets/{self.dataset_id}/pipeline/run",
            _run_body(start_node_id, datasource_type, datasource_info_list, inputs),
        )
        return _ingestion(response.json(), self.dataset_id)

    async def run_draft(
        self,
        *,
        start_node_id: str,
        datasource_type: str,
        datasource_info_list: Sequence[Mapping[str, Any]],
        inputs: Mapping[str, Any] | None = None,
    ) -> WorkflowRun:
        """Run the *draft* pipeline and wait for it, as the console does."""
        response = await self._request(
            "POST",
            f"/datasets/{self.dataset_id}/pipeline/run",
            _run_body(
                start_node_id,
                datasource_type,
                datasource_info_list,
                inputs,
                published=False,
            ),
        )
        return _run_from_blocking(response.json())

    async def stream_draft(
        self,
        *,
        start_node_id: str,
        datasource_type: str,
        datasource_info_list: Sequence[Mapping[str, Any]],
        inputs: Mapping[str, Any] | None = None,
        raise_on_error: bool = True,
    ) -> AsyncWorkflowRunStream:
        """Run the draft pipeline and watch it happen."""
        response = await self._request(
            "POST",
            f"/datasets/{self.dataset_id}/pipeline/run",
            _run_body(
                start_node_id,
                datasource_type,
                datasource_info_list,
                inputs,
                published=False,
                response_mode="streaming",
            ),
            stream=True,
        )
        return AsyncWorkflowRunStream(response, raise_on_error=raise_on_error)

    @staticmethod
    async def upload(
        client: Transport, file: str | Path | BinaryIO, *, filename: str | None = None
    ) -> dict[str, Any]:
        """Upload a file for a pipeline to pick up. Workspace-level."""
        name, content = _part(file, filename or "upload")
        response = await client._send_request_with_files(
            "POST",
            "/datasets/pipeline/file-upload",
            data={},
            files={"file": (name, content)},
        )
        return dict(response.json())
