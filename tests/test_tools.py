"""Tool nodes: discovery on the workspace, authoring offline."""

import httpx
import pytest

from dify_client import DifyManagement
from dify_client.console import CONSOLE_TOKEN_ENV, CONSOLE_URL_ENV
from dify_client.exceptions import ValidationError
from dify_client.tools import ToolCatalog, ToolSpec, parse_provider
from dify_client.workflow import Workflow, WorkflowError, text_input

TOKEN = "ey.FAKE.CONSOLE.TOKEN"

TIME_TOOLS = [
    {
        "name": "current_time",
        "label": {"en_US": "Current Time"},
        "parameters": [
            {
                "name": "format",
                "type": "string",
                "required": False,
                "form": "form",
                "default": "%Y",
            },
            {
                "name": "timezone",
                "type": "select",
                "required": False,
                "form": "form",
                "default": "UTC",
            },
        ],
    },
]
SCRAPER_TOOLS = [
    {
        "name": "webscraper",
        "label": {"en_US": "Web Scraper"},
        "parameters": [
            {"name": "url", "type": "string", "required": True, "form": "llm"},
            {
                "name": "generate_summary",
                "type": "boolean",
                "required": False,
                "form": "form",
                "default": "false",
            },
        ],
    },
]


@pytest.fixture(autouse=True)
def _no_ambient(monkeypatch):
    monkeypatch.delenv(CONSOLE_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CONSOLE_URL_ENV, raising=False)


def catalog() -> ToolCatalog:
    return ToolCatalog(
        [
            parse_provider(
                {"id": "time", "name": "time", "type": "builtin"}, TIME_TOOLS
            ),
            parse_provider(
                {"id": "webscraper", "name": "webscraper", "type": "builtin"},
                SCRAPER_TOOLS,
            ),
            parse_provider(
                {
                    "id": "acme",
                    "name": "acme",
                    "type": "plugin",
                    "plugin_unique_identifier": "vendor/acme:1.0.0@abc",
                },
                [
                    {
                        "name": "lookup",
                        "label": {"en_US": "Lookup"},
                        "parameters": [
                            {
                                "name": "q",
                                "type": "string",
                                "required": True,
                                "form": "llm",
                            }
                        ],
                    }
                ],
            ),
        ]
    )


class TestCatalog:
    def test_providers_index_by_name(self):
        assert catalog()["time"].name == "time"

    def test_an_unknown_provider_lists_what_is_there(self):
        with pytest.raises(KeyError, match="time, webscraper"):
            catalog()["nope"]

    def test_an_unknown_tool_lists_what_the_provider_offers(self):
        with pytest.raises(KeyError, match="current_time"):
            catalog()["time"]["nope"]

    def test_a_tool_knows_which_parameters_go_where(self):
        """Dify marks build-time and per-run parameters differently."""
        scraper = catalog()["webscraper"]["webscraper"]
        assert scraper.configuration_names == ["generate_summary"]
        assert scraper.runtime_names == ["url"]

    def test_labels_come_out_of_the_i18n_object(self):
        assert catalog()["time"]["current_time"].label == "Current Time"

    def test_a_plugin_provider_carries_its_identifier(self):
        assert catalog()["acme"].plugin_unique_identifier == "vendor/acme:1.0.0@abc"

    def test_a_builtin_provider_carries_none(self):
        """Builtins report an empty string; nothing to declare."""
        assert catalog()["time"].plugin_unique_identifier is None

    def test_find_searches_across_providers(self):
        assert [t.provider_name for t in catalog().find("webscraper")] == ["webscraper"]


class TestAuthoring:
    def workflow(self) -> tuple[Workflow, ToolCatalog]:
        wf = Workflow("w")
        wf.start([text_input("url")])
        return wf, catalog()

    def test_a_tool_node_carries_the_identifiers(self):
        wf, cat = self.workflow()
        node = wf.tool(
            cat["time"]["current_time"], config={"timezone": "Asia/Tokyo"}, id="now"
        )
        assert node.type == "tool"
        assert node.data.provider_name == "time"
        assert node.data.tool_name == "current_time"
        assert node.data.tool_configurations == {"timezone": "Asia/Tokyo"}

    def test_the_default_output_of_a_tool_is_text(self):
        """Verified against a running Dify: tool nodes emit text/files/json."""
        wf, cat = self.workflow()
        node = wf.tool(cat["time"]["current_time"], id="now")
        assert node.output.field == "text"

    def test_a_runtime_argument_becomes_a_constant(self):
        wf, cat = self.workflow()
        node = wf.tool(
            cat["webscraper"]["webscraper"], params={"url": "https://x"}, id="p"
        )
        assert node.data.tool_parameters["url"].value == "https://x"
        assert str(node.data.tool_parameters["url"].type) == "constant"

    def test_a_reference_becomes_a_variable(self):
        wf, cat = self.workflow()
        start = wf.nodes[0]
        node = wf.tool(
            cat["webscraper"]["webscraper"], params={"url": start["url"]}, id="p"
        )
        assert list(node.data.tool_parameters["url"].value) == ["start", "url"]
        assert str(node.data.tool_parameters["url"].type) == "variable"

    def test_a_plugin_tool_declares_its_plugin(self):
        """Otherwise Dify imports an app missing what it needs."""
        wf, cat = self.workflow()
        node = wf.tool(cat["acme"]["lookup"], params={"q": "x"}, id="lookup")
        wf.connect(wf.nodes[0], node, wf.end({"out": node.output}))
        assert (
            wf.to_dict()["dependencies"][0]["value"][
                "marketplace_plugin_unique_identifier"
            ]
            == "vendor/acme:1.0.0@abc"
        )
        assert wf.missing_plugin_dependencies() == []

    def test_a_builtin_tool_declares_nothing(self):
        wf, cat = self.workflow()
        node = wf.tool(cat["time"]["current_time"], id="now")
        wf.connect(wf.nodes[0], node, wf.end({"out": node.output}))
        assert "dependencies" not in wf.to_dict()


class TestArgumentChecking:
    def workflow(self):
        wf = Workflow("w")
        wf.start([text_input("url")])
        return wf, catalog()

    def test_an_unknown_parameter_lists_the_real_ones(self):
        wf, cat = self.workflow()
        with pytest.raises(WorkflowError, match="generate_summary, url"):
            wf.tool(cat["webscraper"]["webscraper"], config={"nope": 1})

    def test_a_runtime_parameter_in_config_is_refused(self):
        """Dify accepts the node and then cannot run it, so catch it here."""
        wf, cat = self.workflow()
        with pytest.raises(WorkflowError, match="belongs in params="):
            wf.tool(cat["webscraper"]["webscraper"], config={"url": "x"})

    def test_a_configuration_parameter_in_params_is_refused(self):
        wf, cat = self.workflow()
        with pytest.raises(WorkflowError, match="belongs in config="):
            wf.tool(cat["time"]["current_time"], params={"timezone": "UTC"})

    def test_a_tool_with_no_declared_parameters_is_left_alone(self):
        wf = Workflow("w")
        wf.start()
        spec = ToolSpec(
            provider_id="x", provider_name="x", provider_type="builtin", name="t"
        )
        assert wf.tool(spec, config={"anything": 1}, id="t").type == "tool"


class TestDiscovery:
    def client(self, handler) -> DifyManagement:
        http = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://dify.test/console/api",
        )
        return DifyManagement(
            token=TOKEN, base_url="https://dify.test", http_client=http
        )

    def test_the_catalog_is_assembled_from_two_calls(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/tool-providers"):
                return httpx.Response(
                    200, json=[{"id": "time", "name": "time", "type": "builtin"}]
                )
            if path.endswith("/builtin/time/tools"):
                return httpx.Response(200, json=TIME_TOOLS)
            return httpx.Response(404, json={})

        cat = self.client(handler).tools.catalog()
        assert [t.name for t in cat["time"]] == ["current_time"]

    def test_the_installed_identifier_is_readable(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "plugins": [
                        {
                            "plugin_id": "langgenius/openai",
                            "plugin_unique_identifier": "langgenius/openai:1.0.5@abc",
                        }
                    ]
                },
            )

        assert (
            self.client(handler).tools.identifier("langgenius/openai")
            == "langgenius/openai:1.0.5@abc"
        )

    def test_an_uninstalled_plugin_lists_what_is_installed(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "plugins": [
                        {
                            "plugin_id": "langgenius/openai",
                            "plugin_unique_identifier": "x",
                        }
                    ]
                },
            )

        with pytest.raises(ValidationError, match="langgenius/openai"):
            self.client(handler).tools.identifier("vendor/missing")
