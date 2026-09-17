"""What this SDK deliberately no longer has.

It was built against an old Dify and had accumulated three kinds of dead
weight: classes for endpoints that never existed, methods that pointed console
paths at the Service API, and a client shape that returned raw HTTP responses
for everything. All three are gone; these keep them gone.
"""

import pytest

import dify_client


class TestTheFabricatedClientsAreGone:
    """About 1,100 lines calling routes Dify has never served. Checked against
    all 64 real Service-API routes before deleting."""

    @pytest.mark.parametrize(
        "name",
        [
            "AsyncEnterpriseClient",
            "AsyncSecurityClient",
            "AsyncAnalyticsClient",
            "AsyncIntegrationClient",
            "AsyncAdvancedModelClient",
            "AsyncAdvancedAppClient",
        ],
    )
    def test_it_is_gone(self, name):
        assert not hasattr(dify_client, name)


class TestTheOldClientShapeIsGone:
    """Operations piled onto one class per app mode, every one returning an
    httpx.Response the caller had to unpack by hand."""

    @pytest.mark.parametrize(
        "name",
        [
            "DifyClient",
            "ChatClient",
            "CompletionClient",
            "WorkflowClient",
            "WorkspaceClient",
            "KnowledgeBaseClient",
            "AsyncDifyClient",
            "AsyncChatClient",
            "AsyncCompletionClient",
            "AsyncWorkflowClient",
            "AsyncWorkspaceClient",
            "AsyncKnowledgeBaseClient",
            "client_for",
        ],
    )
    def test_it_is_gone(self, name):
        assert not hasattr(dify_client, name)

    @pytest.mark.parametrize(
        "module", ["dify_client.client", "dify_client.async_client"]
    )
    def test_the_module_is_gone(self, module):
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)

    def test_the_response_models_nothing_used_are_gone(self):
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("dify_client.models")


class TestWhatReplacedThem:
    @pytest.mark.parametrize(
        "name", ["DifyApp", "AsyncDifyApp", "DifyKnowledge", "DifyManagement"]
    )
    def test_the_entry_point_exists(self, name):
        assert hasattr(dify_client, name)

    def test_the_transport_is_not_public(self):
        """It holds the connection, not the vocabulary. Nobody should import it."""
        assert not hasattr(dify_client, "Transport")
        assert "Transport" not in dify_client.__all__

    def test_every_exported_name_resolves(self):
        missing = [n for n in dify_client.__all__ if not hasattr(dify_client, n)]
        assert missing == []

    def test_nothing_public_returns_a_bare_response(self):
        """The old shape's defining property: you got an httpx.Response and
        looked up the keys yourself."""
        import inspect

        from dify_client import DifyApp

        app = DifyApp("k", base_url="https://x/v1")
        for group in (app.chat.messages, app.workflows.runs, app.files):
            for name in dir(group):
                if name.startswith("_"):
                    continue
                annotation = inspect.signature(getattr(group, name)).return_annotation
                assert "Response" not in str(annotation), f"{group}.{name}"
