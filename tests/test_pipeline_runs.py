"""Running a knowledge pipeline: the two shapes that were read wrong.

Both of these were found the first time a pipeline built from code was
deployed and run, because until then there was no way to reach these routes
without a pipeline made by hand in the console.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from dify_client import DifyKnowledge
from dify_client.exceptions import ValidationError


def _client(handler) -> DifyKnowledge:
    return DifyKnowledge(
        "dataset-key",
        base_url="https://dify.test/v1",
        http_client=httpx.Client(
            base_url="https://dify.test/v1", transport=httpx.MockTransport(handler)
        ),
    )


class TestTheDatasourceListing:
    """It is a bare array, and reading it like every other listing crashed."""

    def test_a_bare_array_is_read(self):
        """Dify answers this one with `RootModel[list[...]]` — no `data` key."""
        nodes = [{"node_id": "files", "datasource_type": "local_file"}]

        client = _client(lambda request: httpx.Response(200, json=nodes))
        assert client.pipeline("ds").datasources() == nodes

    def test_an_enveloped_answer_is_read_too(self):
        """The shape is the server's to change, and neither is worth a crash."""
        nodes = [{"node_id": "files"}]

        client = _client(lambda request: httpx.Response(200, json={"data": nodes}))
        assert client.pipeline("ds").datasources() == nodes

    def test_it_asks_about_the_published_pipeline_by_default(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.url.params)
            return httpx.Response(200, json=[])

        _client(handler).pipeline("ds").datasources()
        assert seen["is_published"] == "true"


class TestUploadingAFileForAPipeline:
    """Dify picks the reader by extension, and the name was always 'upload'."""

    def test_the_path_keeps_its_extension(self, tmp_path: Path):
        document = tmp_path / "handbook.md"
        document.write_text("# Handbook", encoding="utf-8")
        sent: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.content.decode("utf-8", "replace")
            sent["body"] = body
            return httpx.Response(200, json={"id": "f1", "name": "handbook.md"})

        _client(handler).upload_for_pipeline(document)
        assert 'filename="handbook.md"' in sent["body"]

    def test_a_name_without_one_is_refused_before_it_is_sent(self):
        """Indexing failed with "Unsupported Extension Type: ." long after the
        upload said 200, so the refusal belongs here."""
        import io

        client = _client(lambda request: httpx.Response(200, json={}))
        with pytest.raises(ValidationError, match="no file extension"):
            client.upload_for_pipeline(io.BytesIO(b"text"))

    def test_an_explicit_filename_is_what_gets_sent(self):
        import io

        sent: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent["body"] = request.content.decode("utf-8", "replace")
            return httpx.Response(200, json={"id": "f1"})

        _client(handler).upload_for_pipeline(io.BytesIO(b"x"), filename="notes.txt")
        assert 'filename="notes.txt"' in sent["body"]

    def test_an_open_file_is_sent_under_its_own_name(self, tmp_path: Path):
        """It was sent as "document" regardless, and then refused for having
        no extension — a perfectly named file, rejected."""
        document = tmp_path / "handbook.md"
        document.write_text("# Handbook", encoding="utf-8")
        sent: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent["body"] = request.content.decode("utf-8", "replace")
            return httpx.Response(200, json={"id": "f1"})

        with document.open("rb") as handle:
            _client(handler).upload_for_pipeline(handle)
        assert 'filename="handbook.md"' in sent["body"]
        assert "# Handbook" in sent["body"]

    def test_a_refused_file_is_not_read(self):
        """The refusal came after the bytes were read, so retrying with the
        same handle and a filename sent nothing."""
        import io

        handle = io.BytesIO(b"text")
        client = _client(lambda request: httpx.Response(200, json={}))
        with pytest.raises(ValidationError):
            client.upload_for_pipeline(handle)
        assert handle.tell() == 0


class TestRunningIt:
    def test_a_published_run_reports_the_batch_it_queued(self):
        answer = {
            "batch": "20260918",
            "dataset": {"id": "ds", "name": "n", "chunk_structure": "text_model"},
            "documents": [
                {"id": "d1", "name": "refunds.txt", "indexing_status": "waiting"}
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert json.loads(request.content)["is_published"] is True
            return httpx.Response(200, json=answer)

        queued = (
            _client(handler)
            .pipeline("ds")
            .run(
                start_node_id="files",
                datasource_type="local_file",
                datasource_info_list=[{"reference": "f1"}],
            )
        )
        assert queued.batch == "20260918"
        # The batch travels with each document, so each can be asked about its
        # own indexing without carrying the batch around separately.
        assert [d.batch for d in queued.documents] == ["20260918"]


class TestListingPipelines:
    """`list()` says "every", and a first page is not every."""

    def _console(self, handler) -> object:
        import httpx

        from dify_client import DifyManagement

        return DifyManagement(
            token="ey-token",
            base_url="https://dify.test",
            http_client=httpx.Client(
                base_url="https://dify.test", transport=httpx.MockTransport(handler)
            ),
        )

    def test_a_pipeline_behind_a_page_of_plain_bases_is_still_found(self):
        """Most knowledge bases are not pipelines, so the one that is can sit
        well past the first page — and reading one page reported none."""
        import httpx

        pages = {
            "1": {
                "data": [{"id": f"d{n}", "name": f"base {n}"} for n in range(30)],
                "has_more": True,
            },
            "2": {
                "data": [{"id": "d99", "name": "pipeline base", "pipeline_id": "p1"}],
                "has_more": False,
            },
        }
        asked: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            page = request.url.params.get("page", "1")
            asked.append(page)
            return httpx.Response(200, json=pages[page])

        found = self._console(handler).pipelines.list()

        assert [p.id for p in found] == ["p1"]
        assert asked == ["1", "2"]

    def test_a_server_that_never_says_stop_is_reported_rather_than_truncated(self):
        """A listing that quietly stops early is the bug `Page.all()` exists
        for, so the walk raises and carries what it read."""
        import httpx
        import pytest

        from dify_client.results import MAX_WALK, PageLimitReached

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "d", "name": "n", "pipeline_id": "p"}] * 100,
                    "has_more": True,
                },
            )

        with pytest.raises(PageLimitReached) as reached:
            self._console(handler).pipelines.list()

        assert len(reached.value.items) >= MAX_WALK
