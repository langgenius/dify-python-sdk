"""Things a consumer of the installed package depends on.

Type annotations reach nobody without a PEP 561 marker, and a client that
builds its own httpx.Client cannot be pointed at a proxy, a corporate TLS
bundle, or a test transport.
"""

from pathlib import Path

import httpx
import pytest

from dify_client import AsyncDifyApp, DifyApp, DifyKnowledge
from dify_client._async_transport import AsyncTransport
from dify_client._transport import Transport

ROOT = Path(__file__).resolve().parent.parent
SYNC_CLIENTS = [Transport, DifyApp, DifyKnowledge]


class TestTypesAreShipped:
    def test_the_marker_exists(self):
        """Without it, mypy and pyright ignore every annotation in the package."""
        assert (ROOT / "dify_client" / "py.typed").is_file()

    def test_the_build_is_told_to_include_it(self):
        pyproject = (ROOT / "pyproject.toml").read_text()
        assert "dify_client/py.typed" in pyproject

    def test_it_lands_in_the_wheel(self):
        """A marker the build drops is a marker that does nothing."""
        import zipfile

        wheels = sorted((ROOT / "dist").glob("*.whl"))
        if not wheels:
            pytest.skip("no wheel built")
        with zipfile.ZipFile(wheels[-1]) as wheel:
            assert "dify_client/py.typed" in wheel.namelist()


class TestInjectingAnHttpClient:
    """DifyManagement and OpenApiClient took one; the Service-API clients did not."""

    def _stub(self):
        def handler(request):
            return httpx.Response(200, json={"seen": str(request.url)})

        return httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://injected/v1"
        )

    @pytest.mark.parametrize("cls", SYNC_CLIENTS, ids=lambda c: c.__name__)
    def test_every_client_accepts_one(self, cls):
        """Checked by using it, since DifyKnowledge forwards **kwargs."""
        client = cls("k", http_client=self._stub())
        assert "injected" in str(client._client.base_url)

    def test_the_injected_transport_is_the_one_used(self):
        app = DifyApp("k", http_client=self._stub())
        assert "injected" in app._send_request("GET", "/info").json()["seen"]

    def test_the_async_client_takes_one_too(self):
        import asyncio

        def handler(request):
            return httpx.Response(200, json={"seen": str(request.url)})

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://injected/v1"
        )
        client = AsyncTransport("k", http_client=http)
        result = asyncio.run(client._send_request("GET", "/info"))
        assert "injected" in result.json()["seen"]

    def test_none_still_builds_the_default(self):
        client = DifyApp("k", base_url="https://api.dify.ai/v1")
        assert str(client._client.base_url).rstrip("/") == "https://api.dify.ai/v1"


class TestDifyKnowledgeTakesTheSameKnobs:
    """Its own __init__ dropped every tuning argument the others accept."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"timeout": 5.0},
            {"max_retries": 7},
            {"retry_delay": 0.5},
            {"enable_logging": True},
        ],
        ids=lambda k: next(iter(k)),
    )
    def test_it_accepts(self, kwargs):
        DifyKnowledge("k", base_url="https://x/v1", **kwargs)

    def test_retries_are_configurable(self):
        client = DifyKnowledge("k", base_url="https://x/v1", max_retries=7)
        assert client.max_retries == 7

    def test_a_transport_can_be_injected(self):
        def handler(request):
            return httpx.Response(200, json={"data": []})

        knowledge = DifyKnowledge(
            "k",
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler), base_url="https://x/v1"
            ),
        )
        assert list(knowledge.datasets.list()) == []

    def test_the_async_twin_matches(self):
        import inspect

        assert "kwargs" in inspect.signature(AsyncDifyApp.__init__).parameters

    def test_a_dataset_is_named_per_call_not_per_client(self):
        """It used to be a constructor argument, so one client meant one
        dataset and half the methods silently needed it."""
        knowledge = DifyKnowledge("k", base_url="https://x/v1")
        assert knowledge.documents("ds-1").dataset_id == "ds-1"
        assert knowledge.documents("ds-2").dataset_id == "ds-2"


class TestVersion:
    """The changelog and the package have to agree on what is being released."""

    def _version(self):
        import tomllib

        return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
            "version"
        ]

    def test_the_changelog_heads_with_the_packaged_version(self):
        changelog = (ROOT / "CHANGELOG.md").read_text()
        first = next(line for line in changelog.splitlines() if line.startswith("## "))
        assert self._version() in first

    def test_the_installed_package_reports_it(self):
        import dify_client

        assert dify_client.__version__ == self._version()

    def test_a_breaking_rewrite_is_not_a_minor_bump(self):
        """The published API is replaced, not extended: `pip install -U` on
        the old one breaks working code, and the number has to say so."""
        major, _, _ = self._version().partition(".")
        assert int(major) >= 1, (
            "0.x invites an in-place upgrade over a rewrite that removes every "
            "client the previous release documented"
        )

    def test_the_migration_guide_exists(self):
        """Breaking changes with nowhere to read about them are a trap."""
        changelog = (ROOT / "CHANGELOG.md").read_text()
        assert "### Migrating" in changelog
        assert "### Removed" in changelog
