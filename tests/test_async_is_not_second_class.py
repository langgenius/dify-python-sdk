"""Whatever the sync client can do, the async client can do.

Async used to be a subset: knowledge had no async client at all, half of
``DifyApp``'s own methods had no counterpart, and the async listings returned a
page that could not fetch the next one — so ``all()`` existed on one path and
not the other. Each of those was found by a caller, not by a test. These are
the tests.
"""

import asyncio
import inspect
import json

import httpx
import pytest

from dify_client import (
    AsyncDifyApp,
    AsyncDifyKnowledge,
    AsyncPage,
    DifyApp,
    DifyKnowledge,
    Page,
)
from dify_client.resources import knowledge as knowledge_resources

from .test_resource_api import Dify


def _pairs():
    """The sync class and its async counterpart, for each resource.

    Discovered rather than listed: a hand-written list is how
    ``AsyncFiles.preview_url`` came to be missing — the resource was simply not
    on it, so nothing checked it.
    """
    from dify_client import resources

    found = [(DifyApp, AsyncDifyApp), (DifyKnowledge, AsyncDifyKnowledge)]
    for module in (resources, knowledge_resources):
        for name in dir(module):
            if not name.startswith("Async"):
                continue
            asynchronous = getattr(module, name)
            sync = getattr(module, name[len("Async") :], None)
            if isinstance(asynchronous, type) and isinstance(sync, type):
                found.append((sync, asynchronous))
    return sorted(set(found), key=lambda pair: pair[0].__name__)


def _verbs(cls):
    """The public methods of a resource, ignoring what every object has."""
    return {
        name
        for name, member in inspect.getmembers(cls, callable)
        if not name.startswith("_") and name not in vars(object)
    }


def test_every_async_resource_is_paired():
    """If this drops, the parametrised checks below are checking less."""
    names = {sync.__name__ for sync, _ in _pairs()}
    assert {"Files", "Messages", "Conversations", "WorkflowRuns", "Forms"} <= names
    assert len(_pairs()) >= 12


@pytest.mark.parametrize("sync, asynchronous", _pairs(), ids=lambda c: c.__name__)
class TestEveryVerbHasACounterpart:
    def test_nothing_is_missing(self, sync, asynchronous):
        # `close` is spelled `aclose` when it has to be awaited; everything
        # else keeps its name, so porting is a matter of adding `await`.
        counterparts = {"close": "aclose"}
        missing = {
            counterparts.get(verb, verb)
            for verb in _verbs(sync)
            if counterparts.get(verb, verb) not in _verbs(asynchronous)
        }
        assert not missing, f"{asynchronous.__name__} cannot {sorted(missing)}"

    def test_nothing_is_invented(self, sync, asynchronous):
        """The async client is the same API, not a different one."""
        extra = _verbs(asynchronous) - _verbs(sync) - {"aclose"}
        assert not extra, f"{asynchronous.__name__} has {sorted(extra)}, sync does not"

    def test_the_arguments_match(self, sync, asynchronous):
        """Same names, same order, same defaults — or a caller cannot port."""
        for verb in sorted(_verbs(sync) & _verbs(asynchronous)):
            if verb in {"open", "probe", "upload"}:
                continue  # constructors and staticmethods, checked separately
            expected = inspect.signature(getattr(sync, verb))
            actual = inspect.signature(getattr(asynchronous, verb))
            assert str(actual.parameters) == str(expected.parameters), verb


class TestTheClientsHaveTheSameEntryPoints:
    def test_both_apps_open_with_a_check(self):
        assert inspect.iscoroutinefunction(AsyncDifyApp.open)
        assert not inspect.iscoroutinefunction(DifyApp.open)

    def test_both_apps_probe_without_a_credential(self):
        assert inspect.iscoroutinefunction(AsyncDifyApp.probe)

    def test_a_knowledge_client_closes_either_way(self):
        assert hasattr(DifyKnowledge, "close")
        assert hasattr(AsyncDifyKnowledge, "aclose")


PAGE_ONE = {
    "data": [{"id": "a", "name": "first"}],
    "has_more": True,
    "limit": 1,
    "total": 2,
}
PAGE_TWO = {"data": [{"id": "b", "name": "second"}], "has_more": False, "limit": 1}


def _two_pages(payloads):
    """A handler that answers the first request, then the second, then 404s."""
    remaining = list(payloads)

    def reply():
        return httpx.Response(200, json=remaining.pop(0))

    return reply


class TestAnAsyncListingWalksItself:
    """``all()`` is the loop callers were writing by hand. It exists on both."""

    def _knowledge(self, dify):
        return AsyncDifyKnowledge(
            "dataset-key",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(dify.handler),
                base_url="https://dify.test/v1",
            ),
        )

    def test_the_first_page_is_what_dify_sent(self):
        dify = Dify(**{"/datasets": _two_pages([PAGE_ONE, PAGE_TWO])})

        async def call():
            async with self._knowledge(dify) as knowledge:
                return await knowledge.datasets.list(limit=1)

        page = asyncio.run(call())
        assert isinstance(page, AsyncPage)
        assert [d.name for d in page] == ["first"]
        assert page.has_more and page.total == 2

    def test_all_fetches_what_follows(self):
        dify = Dify(**{"/datasets": _two_pages([PAGE_ONE, PAGE_TWO])})

        async def call():
            async with self._knowledge(dify) as knowledge:
                page = await knowledge.datasets.list(limit=1)
                return [d.name async for d in page.all()]

        assert asyncio.run(call()) == ["first", "second"]
        assert [call["params"]["page"] for call in dify.calls] == ["1", "2"]

    def test_the_last_page_says_there_is_no_next(self):
        dify = Dify(**{"/datasets": _two_pages([PAGE_TWO])})

        async def call():
            async with self._knowledge(dify) as knowledge:
                page = await knowledge.datasets.list(limit=1)
                return await page.next_page()

        assert asyncio.run(call()) is None

    def test_a_listing_dify_does_not_page_is_still_a_page(self):
        """Tags come back as a bare array. ``all()`` works on it anyway."""
        dify = Dify(
            **{"/datasets/tags": httpx.Response(200, json=[{"id": "t", "name": "x"}])}
        )

        async def call():
            async with self._knowledge(dify) as knowledge:
                page = await knowledge.tags.list()
                return [tag.name async for tag in page.all()]

        assert asyncio.run(call()) == ["x"]

    def test_both_page_types_read_the_same(self):
        """Different types, same vocabulary — so porting is mechanical."""
        import dataclasses

        facts = {"items", "has_more", "limit", "total"}
        verbs = {"next_page", "all", "pages"}
        for kind in (Page, AsyncPage):
            assert facts <= {f.name for f in dataclasses.fields(kind)}
            assert verbs <= set(dir(kind))


class TestAPublishedPipelineRunIsQueuedWork:
    """Dify does two different things behind one route, and says so in the
    response: a published run enqueues documents, a draft run executes the
    graph. The SDK used to return `Any` — a dict or a raw httpx response —
    and leave the caller to notice."""

    QUEUED = {
        "batch": "20260101120000123456",
        "dataset": {"id": "ds-1", "name": "handbook"},
        "documents": [
            {"id": "doc-1", "name": "page one", "indexing_status": "waiting"},
            {"id": "doc-2", "name": "page two", "indexing_status": "waiting"},
        ],
    }

    def _knowledge(self, dify):
        return DifyKnowledge(
            "dataset-key",
            http_client=httpx.Client(
                transport=httpx.MockTransport(dify.handler),
                base_url="https://dify.test/v1",
            ),
        )

    def _run(self, dify, **kwargs):
        with self._knowledge(dify) as knowledge:
            return knowledge.pipeline("ds-1").run(
                start_node_id="start",
                datasource_type="local_file",
                datasource_info_list=[{"file_id": "f-1"}],
                **kwargs,
            )

    def test_it_reports_the_documents_it_queued(self):
        dify = Dify(
            **{"/datasets/ds-1/pipeline/run": httpx.Response(200, json=self.QUEUED)}
        )
        queued = self._run(dify)

        assert [d.name for d in queued.documents] == ["page one", "page two"]
        assert len(queued) == 2

    def test_each_document_carries_the_batch_it_arrived_in(self):
        """Otherwise `indexing_status()` asks about `/documents//indexing-status`."""
        dify = Dify(
            **{"/datasets/ds-1/pipeline/run": httpx.Response(200, json=self.QUEUED)}
        )
        queued = self._run(dify)

        assert queued.batch == "20260101120000123456"
        assert {d.batch for d in queued.documents} == {queued.batch}

    def test_nothing_is_indexed_yet(self):
        dify = Dify(
            **{"/datasets/ds-1/pipeline/run": httpx.Response(200, json=self.QUEUED)}
        )

        assert not any(d.indexed for d in self._run(dify).documents)

    def test_it_asks_for_the_published_pipeline(self):
        dify = Dify(
            **{"/datasets/ds-1/pipeline/run": httpx.Response(200, json=self.QUEUED)}
        )
        self._run(dify)

        assert dify.calls[0]["body"]["is_published"] is True

    def test_a_draft_run_is_a_workflow_run(self):
        """Dify runs the draft graph through the workflow engine and reports it
        as one, so this is the same type as `app.workflows.runs.create`."""
        blocking = {
            "task_id": "task-1",
            "data": {"id": "run-1", "status": "succeeded", "outputs": {"x": 1}},
        }
        dify = Dify(
            **{"/datasets/ds-1/pipeline/run": httpx.Response(200, json=blocking)}
        )
        with self._knowledge(dify) as knowledge:
            run = knowledge.pipeline("ds-1").run_draft(
                start_node_id="start",
                datasource_type="local_file",
                datasource_info_list=[{"file_id": "f-1"}],
            )

        assert run.succeeded
        assert run.outputs == {"x": 1}
        assert dify.calls[0]["body"]["is_published"] is False

    def test_a_draft_run_can_be_watched(self):
        events = [
            {"event": "workflow_started", "task_id": "t", "data": {"id": "run-1"}},
            {
                "event": "workflow_finished",
                "task_id": "t",
                "data": {"status": "succeeded", "outputs": {"x": 1}},
            },
        ]
        body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
        dify = Dify(**{"/datasets/ds-1/pipeline/run": httpx.Response(200, text=body)})

        with self._knowledge(dify) as knowledge:
            stream = knowledge.pipeline("ds-1").stream_draft(
                start_node_id="start",
                datasource_type="local_file",
                datasource_info_list=[{"file_id": "f-1"}],
            )
            seen = [event.type for event in stream]
            run = stream.get_final_run()

        assert seen[0] == "workflow_started"
        assert run.succeeded
        assert dify.calls[0]["body"]["response_mode"] == "streaming"

    def test_a_base_without_a_pipeline_says_so(self):
        dify = Dify(
            **{
                "/datasets/ds-1/pipeline/run": httpx.Response(
                    404, json={"message": "Pipeline not found"}
                )
            }
        )
        from dify_client.exceptions import APIError

        with pytest.raises(APIError, match="has no RAG pipeline"):
            self._run(dify)
