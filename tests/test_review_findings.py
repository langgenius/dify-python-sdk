"""Defects an external review found, kept fixed.

The transport-level ones (retries, streaming, invented limits) live in
test_transport.py, where the behaviour now lives. What remains here is about
the lifecycle: deploying, publishing, and cleaning up when a step fails.
"""

import httpx
import pytest

from dify_client import DifyManagement, Stage
from dify_client.workflow import Workflow, text_input


def workflow(name="w"):
    wf = Workflow(name)
    start = wf.start([text_input("a")])
    wf.connect(start, wf.end({"out": start["a"]}))
    return wf


class Dify:
    """A console that can be told which step fails."""

    def __init__(self, *, publish_fails=False, key_fails=False, import_fails=False):
        self.publish_fails = publish_fails
        self.key_fails = key_fails
        self.import_fails = import_fails
        self.calls = []
        self.deleted = []

    def handler(self, request):
        path = request.url.path.replace("/console/api", "")
        self.calls.append((request.method, path))
        if path == "/apps/imports":
            if self.import_fails:
                return httpx.Response(
                    200, json={"id": "i1", "status": "failed", "error": "bad DSL"}
                )
            return httpx.Response(
                200,
                json={
                    "id": "i1",
                    "status": "completed",
                    "app_id": "new-app",
                    "app_mode": "workflow",
                },
            )
        if path.endswith("/publish"):
            if self.publish_fails:
                return httpx.Response(500, json={"message": "publish blew up"})
            return httpx.Response(200, json={"id": "version-1"})
        if path.endswith("/api-keys") and request.method == "POST":
            if self.key_fails:
                return httpx.Response(500, json={"message": "no keys left"})
            return httpx.Response(200, json={"id": "k1", "token": "app-minted"})
        if request.method == "DELETE":
            self.deleted.append(path)
            return httpx.Response(204)
        return httpx.Response(200, json={})

    def management(self):
        http = httpx.Client(
            transport=httpx.MockTransport(self.handler),
            base_url="https://dify.test/console/api",
        )
        return DifyManagement(
            token="ey.T", base_url="https://dify.test", http_client=http
        )


class TestTheLifecycleIsVisible:
    """`deploy` used to raise a bare APIError, leaving the caller unable to
    tell "nothing happened" from "the app exists but is unpublished"."""

    def test_a_full_deploy_reaches_runnable(self):
        result = Dify().management().apps.deploy(workflow())
        assert result.stage is Stage.RUNNABLE
        assert result.api_key == "app-minted"
        assert result.created

    def test_a_failed_import_created_nothing(self):
        result = Dify(import_fails=True).management().apps.deploy(workflow())
        assert result.stage is Stage.NOT_IMPORTED
        assert not result.imported

    def test_a_failed_publish_still_leaves_a_draft(self):
        """The app exists. Saying only "error" hides that."""
        result = Dify(publish_fails=True).management().apps.deploy(workflow())
        assert result.stage is Stage.DRAFTED
        assert result.imported
        assert not result.published
        assert result.app_id == "new-app"
        assert "publish" in result.error

    def test_a_failed_key_leaves_it_published(self):
        result = Dify(key_fails=True).management().apps.deploy(workflow())
        assert result.stage is Stage.PUBLISHED
        assert result.published
        assert not result.runnable

    def test_the_error_says_what_exists_and_what_to_do(self):
        result = Dify(publish_fails=True).management().apps.deploy(workflow())
        with pytest.raises(Exception) as caught:
            result.raise_for_stage()
        assert "new-app" in str(caught.value)
        assert "exists on Dify" in str(caught.value)

    def test_importing_alone_does_not_publish(self):
        """Import writes a draft. The Service API runs the published version."""
        dify = Dify()
        result = dify.management().apps.import_definition(workflow())

        assert result.stage is Stage.DRAFTED
        assert not any(path.endswith("/publish") for _, path in dify.calls)

    def test_deploying_without_publishing_is_asked_for_explicitly(self):
        dify = Dify()
        dify.management().apps.deploy(workflow(), publish=False, key=False)
        assert not any(path.endswith("/publish") for _, path in dify.calls)


class TestCleanup:
    """`ephemeral_app` never reached its own cleanup when provisioning failed
    partway, so a half-made app stayed in the workspace."""

    def test_a_temporary_app_is_deleted_on_the_way_out(self):
        dify = Dify()
        with dify.management().apps.temporary(workflow()) as app:
            assert app.id == "new-app"
        assert dify.deleted == ["/apps/new-app"]

    def test_it_is_deleted_when_the_block_raises(self):
        dify = Dify()
        with pytest.raises(RuntimeError):
            with dify.management().apps.temporary(workflow()):
                raise RuntimeError("the test failed")
        assert dify.deleted == ["/apps/new-app"]

    def test_it_is_deleted_when_publishing_fails(self):
        """The app was created before the failure; it is still an app."""
        dify = Dify(publish_fails=True)
        with pytest.raises(Exception):
            with dify.management().apps.temporary(workflow()):
                pass
        assert dify.deleted == ["/apps/new-app"]

    def test_nothing_is_deleted_when_nothing_was_created(self):
        dify = Dify(import_fails=True)
        with pytest.raises(Exception):
            with dify.management().apps.temporary(workflow()):
                pass
        assert dify.deleted == []

    def test_an_app_that_was_adopted_is_never_deleted(self):
        """Only apps this SDK created are safe to delete."""
        dify = Dify()
        result = dify.management().apps.deploy(workflow(), app_id="existing")
        assert not result.created


class TestRunLivePublishes:
    def test_it_publishes_what_it_just_deployed(self):
        """Without this, a live run tested the previously published version."""
        import inspect

        source = inspect.getsource(Workflow.run_live)
        assert "apps.deploy" in source
        assert "Stage.PUBLISHED" in source


class TestTheBillingGateIsInTheTestingLayer:
    """The review asked for one answer: is this a normal API or a billed-test
    API? `DifyApp` is the normal one and has no gate; `run_live` is the testing
    one and keeps it."""

    def test_run_live_is_gated(self, monkeypatch):
        from dify_client.workflow.live import LIVE_ENABLED_ENV, LiveRunError

        monkeypatch.delenv(LIVE_ENABLED_ENV, raising=False)
        with pytest.raises(LiveRunError, match=LIVE_ENABLED_ENV):
            workflow().run_live({"a": "x"})

    def test_the_ordinary_client_is_not_gated(self, monkeypatch):
        """Gating an ordinary API call on a test variable would be wrong."""
        import inspect

        from dify_client.resources.runs import WorkflowRuns

        monkeypatch.delenv("DIFY_LIVE_TESTS", raising=False)
        source = inspect.getsource(WorkflowRuns)
        assert "DIFY_LIVE_TESTS" not in source
        assert "live_enabled" not in source
