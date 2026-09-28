"""Agents as code.

The soul's schema lives in the Dify server and is not published, so this SDK
carries it without interpreting it. These tests cover what can be enforced
without the schema: the envelope Dify expects, a faithful round trip, and that
nothing credential-shaped reaches a file.
"""

import json

import httpx
import pytest
import yaml

from dify_client import Agent, AgentError, DifyManagement
from dify_client.agent import strip_sensitive
from dify_client.console import CONSOLE_TOKEN_ENV, CONSOLE_URL_ENV
from dify_client.exceptions import ValidationError
from dify_client.workflow import Workflow, text_input

TOKEN = "ey.FAKE.CONSOLE.TOKEN.000000"
APP_ID = "11111111-2222-3333-4444-555555555555"

# Shaped the way Dify's own models are: credentials live in credential_ref, in
# a tool's runtime_parameters, and in file ids — not as loose keys.
SOUL = {
    "prompt": {"system_prompt": "Triage inbound support messages."},
    "model": {
        "plugin_id": "langgenius/openai",
        "model_provider": "langgenius/openai/openai",
        "model": "gpt-4o-mini",
        "credential_ref": "cred-abc123",
        "model_settings": {"temperature": 0.7},
    },
    "tools": {
        "dify_tools": [
            {
                "tool_name": "search",
                "provider": "search",
                "credential_type": "api-key",
                "credential_ref": "cred-tool-1",
                "runtime_parameters": {"api_key": "sk-LEAKME", "region": "jp"},
            }
        ],
        "cli_tools": [],
    },
    "env": {"secret_refs": [], "variables": []},
    "config_files": [{"name": "runbook.md", "file_id": "file-xyz"}],
}


@pytest.fixture(autouse=True)
def _no_ambient_console_config(monkeypatch):
    monkeypatch.delenv(CONSOLE_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CONSOLE_URL_ENV, raising=False)


def triage() -> Agent:
    return Agent(
        "support-triage",
        soul=SOUL,
        description="Triage inbound support messages.",
        role="Support engineer",
    )


class TestStripSensitive:
    """The rule is by key name, so it works on a soul we do not understand."""

    def test_credentials_are_blanked(self):
        assert strip_sensitive({"credential_ref": "x"}) == {"credential_ref": None}
        assert strip_sensitive({"api_key": "x"}) == {"api_key": None}
        assert strip_sensitive({"access_token": "x"}) == {"access_token": None}
        assert strip_sensitive({"my_secret_thing": "x"}) == {"my_secret_thing": None}
        assert strip_sensitive({"upload_file_id": "x"}) == {"upload_file_id": None}

    def test_ordinary_values_survive(self):
        assert strip_sensitive({"name": "search", "temperature": 0.7}) == {
            "name": "search",
            "temperature": 0.7,
        }

    def test_it_reaches_into_nested_structures(self):
        nested = {"tools": [{"inner": {"api_key": "x", "name": "keep"}}]}
        assert strip_sensitive(nested) == {
            "tools": [{"inner": {"api_key": None, "name": "keep"}}]
        }

    def test_the_original_is_untouched(self):
        original = {"api_key": "x"}
        strip_sensitive(original)
        assert original == {"api_key": "x"}


class TestExport:
    def test_it_emits_the_envelope_dify_expects(self):
        doc = triage().to_dict()
        assert doc["kind"] == "app"
        assert doc["app"]["mode"] == "agent"
        assert doc["agent"]["package_ref"] == "agent_1"
        package = doc["agent_packages"]["agent_1"]
        assert package["schema_version"] == 1
        assert {"schema_version", "metadata", "soul"} <= set(package)

    def test_the_metadata_carries_the_role(self):
        metadata = triage().to_dict()["agent_packages"]["agent_1"]["metadata"]
        assert metadata["name"] == "support-triage"
        assert metadata["role"] == "Support engineer"

    def test_credentials_never_reach_the_yaml(self):
        text = triage().to_yaml()
        assert "sk-LEAKME" not in text
        assert "cred-abc123" not in text
        assert "cred-tool-1" not in text
        assert "file-xyz" not in text

    def test_what_is_not_a_secret_survives(self):
        soul = yaml.safe_load(triage().to_yaml())["agent_packages"]["agent_1"]["soul"]
        assert soul["model"]["model"] == "gpt-4o-mini"
        assert soul["model"]["model_settings"]["temperature"] == 0.7
        assert soul["tools"]["dify_tools"][0]["tool_name"] == "search"
        assert soul["tools"]["dify_tools"][0]["runtime_parameters"]["region"] == "jp"

    def test_include_secret_is_an_explicit_opt_in(self):
        assert "sk-LEAKME" in triage().to_yaml(include_secret=True)

    def test_exporting_does_not_mutate_the_agent(self):
        agent = triage()
        agent.to_yaml()
        assert agent.soul["model"]["credential_ref"] == "cred-abc123"
        assert (
            agent.soul["tools"]["dify_tools"][0]["runtime_parameters"]["api_key"]
            == "sk-LEAKME"
        )

    def test_it_writes_the_file(self, tmp_path):
        path = tmp_path / "agent.yml"
        triage().to_yaml(path)
        assert yaml.safe_load(path.read_text())["app"]["mode"] == "agent"

    def test_an_agent_without_a_soul_says_where_to_get_one(self):
        with pytest.raises(AgentError, match="apps.export"):
            Agent("empty").to_dict()

    def test_an_agent_without_a_name_is_rejected(self):
        with pytest.raises(AgentError, match="needs a name"):
            Agent("", soul=SOUL).to_dict()


class TestForwardCompatibility:
    """Dify's AgentPackage forbids unknown fields, and fields arrive over time.

    A server that predates `workspace_skills` rejects the whole import if the
    key is present, so an empty one is left out entirely.
    """

    def test_empty_later_fields_are_omitted(self):
        package = triage().to_dict()["agent_packages"]["agent_1"]
        assert "workspace_skills" not in package
        assert "omitted_assets" not in package

    def test_they_are_emitted_once_they_hold_something(self):
        agent = triage()
        agent.workspace_skills = [{"name": "lookup", "priority": 0}]
        package = agent.to_dict()["agent_packages"]["agent_1"]
        assert package["workspace_skills"] == [{"name": "lookup", "priority": 0}]

    def test_the_envelope_is_otherwise_complete(self):
        package = triage().to_dict()["agent_packages"]["agent_1"]
        assert set(package) == {"schema_version", "metadata", "soul"}


class TestRoundTrip:
    def test_an_exported_agent_reads_back(self):
        agent = Agent.from_yaml(triage().to_yaml(include_secret=True))
        assert agent.name == "support-triage"
        assert agent.role == "Support engineer"
        assert agent.soul == SOUL

    def test_a_stripped_export_reads_back_with_the_blanks(self):
        """The shape survives even when the values were removed."""
        agent = Agent.from_yaml(triage().to_yaml())
        assert agent.soul["model"]["credential_ref"] is None
        assert agent.soul["model"]["model"] == "gpt-4o-mini"

    def test_editing_the_soul_is_just_editing_a_dict(self):
        agent = Agent.from_yaml(triage().to_yaml(include_secret=True))
        agent.soul["model"]["model_settings"]["temperature"] = 0.1
        reloaded = Agent.from_yaml(agent.to_yaml(include_secret=True))
        assert reloaded.soul["model"]["model_settings"]["temperature"] == 0.1

    def test_it_reads_from_a_path(self, tmp_path):
        path = tmp_path / "agent.yml"
        triage().to_yaml(path, include_secret=True)
        assert Agent.from_yaml(path).soul == SOUL

    def test_a_workflow_dsl_is_rejected_with_a_pointer(self):
        wf = Workflow("w")
        start = wf.start([text_input("q")])
        wf.connect(start, wf.answer(start["q"]))
        with pytest.raises(AgentError, match="Workflow"):
            Agent.from_yaml(wf.to_yaml())

    def test_a_document_without_a_package_says_what_is_missing(self):
        with pytest.raises(AgentError, match="agent_packages"):
            Agent.from_dict({"app": {"mode": "agent", "name": "x"}, "kind": "app"})

    def test_something_that_is_not_a_dsl_is_rejected(self):
        with pytest.raises(AgentError, match="not a Dify DSL export"):
            Agent.from_dict({"hello": "world"})


class TestThroughTheConsole:
    def client(self, handler) -> DifyManagement:
        http = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://dify.test/console/api",
        )
        return DifyManagement(
            token=TOKEN, base_url="https://dify.test", http_client=http
        )

    def test_export_returns_the_dsl(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith(f"/apps/{APP_ID}/export")
            assert request.url.params["include_secret"] == "false"
            return httpx.Response(200, json={"data": triage().to_yaml()})

        text = self.client(handler).apps.export(APP_ID)
        assert Agent.from_yaml(text).name == "support-triage"

    def test_export_can_ask_for_secrets(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["include_secret"] = request.url.params["include_secret"]
            return httpx.Response(200, json={"data": "app: {}"})

        with pytest.raises(AgentError):
            Agent.from_yaml(
                self.client(handler).apps.export(APP_ID, include_secret=True)
            )
        assert seen["include_secret"] == "true"

    def test_an_empty_export_is_an_error(self):
        def handler(request):
            return httpx.Response(200, json={"data": ""})

        with pytest.raises(ValidationError, match="no DSL"):
            self.client(handler).apps.export(APP_ID)

    def test_deploying_an_agent_sends_agent_dsl(self):
        sent = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content))
            return httpx.Response(
                200, json={"id": "i1", "status": "completed", "app_id": APP_ID}
            )

        self.client(handler).apps.deploy(triage())
        document = yaml.safe_load(sent["yaml_content"])
        assert document["app"]["mode"] == "agent"
        assert "sk-LEAKME" not in sent["yaml_content"]

    def test_overwriting_an_agent_is_refused_before_the_request(self):
        """Dify's importer only creates new Agent apps; say so up front."""

        def handler(request):
            raise AssertionError("no request should have been made")

        with pytest.raises(ValidationError, match="only creates new Agent apps"):
            self.client(handler).apps.deploy(triage(), app_id=APP_ID)

    def _console_that_deploys_an_agent(self, calls):
        """A Dify that answers the four calls deploying an Agent makes."""

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.replace("/console/api", "")
            calls.append(path)
            if path == "/apps/imports":
                # Dify reports the mode it imported. That is what the SDK
                # reads, because the definition it was handed may be a string.
                return httpx.Response(
                    200,
                    json={
                        "id": "i",
                        "status": "completed",
                        "app_id": APP_ID,
                        "app_mode": "agent",
                    },
                )
            if path == "/agent":
                # The roster, where an Agent is keyed by its own id rather
                # than by the app it is bound to.
                return httpx.Response(
                    200,
                    json={
                        "data": [{"id": "agent-1", "app_id": APP_ID, "name": "triage"}]
                    },
                )
            if path == "/agent/agent-1/publish":
                return httpx.Response(200, json={"active_config_snapshot_id": "snap-1"})
            if path.endswith("/api-keys"):
                return httpx.Response(
                    200,
                    json={"id": "k", "token": "app-AGENTKEY12345678", "type": "app"},
                )
            return httpx.Response(200, json={})

        return handler

    def test_provisioning_an_agent_publishes_through_the_roster(self):
        """Not through `/workflows/publish`, which Dify serves for workflow
        and advanced-chat only — and not "nothing to publish" either, which is
        what this used to assert: a real Dify answers the key request with
        "Publish the Agent before enabling Web App or API access"."""
        calls = []
        app = self.client(self._console_that_deploys_an_agent(calls)).apps.deploy(
            triage()
        )

        assert "/agent/agent-1/publish" in calls
        assert not any("workflows/publish" in path for path in calls)
        assert app.published
        assert app.api_key == "app-AGENTKEY12345678"

    def test_an_older_dify_that_reports_no_mode_still_works_from_the_object(self):
        """The Agent object knows what it is, and is used when Dify's import
        answer does not say."""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.replace("/console/api", "")
            calls.append(path)
            if path == "/apps/imports":
                return httpx.Response(
                    200, json={"id": "i", "status": "completed", "app_id": APP_ID}
                )
            if path == "/agent":
                return httpx.Response(
                    200, json={"data": [{"id": "agent-1", "app_id": APP_ID}]}
                )
            if path.endswith("/api-keys"):
                return httpx.Response(
                    200, json={"id": "k", "token": "app-K", "type": "app"}
                )
            return httpx.Response(200, json={})

        self.client(handler).apps.deploy(triage())
        assert "/agent/agent-1/publish" in calls

    def test_the_same_agent_as_dsl_takes_the_same_path(self):
        """`deploy()` also takes DSL as a string, and a string has no `.mode`
        — which is what used to decide whether this was an Agent at all."""
        calls = []
        app = self.client(self._console_that_deploys_an_agent(calls)).apps.deploy(
            triage().to_yaml()
        )

        assert "/agent/agent-1/publish" in calls
        assert app.api_key == "app-AGENTKEY12345678"


SPEC_TOOL = {
    "name": "current_time",
    "label": {"en_US": "Current Time"},
    "parameters": [
        {
            "name": "timezone",
            "type": "select",
            "required": False,
            "form": "form",
            "default": "UTC",
        }
    ],
}


def a_tool_spec(plugin: str | None = None):
    from dify_client.tools import parse_provider

    payload = {"id": "time", "name": "time", "type": "builtin"}
    if plugin:
        payload["plugin_unique_identifier"] = plugin
    return parse_provider(payload, [SPEC_TOOL])["current_time"]


class TestCreating:
    """Building an Agent from scratch rather than from an export."""

    def test_the_default_soul_is_the_shape_dify_writes(self):
        from dify_client.agent import default_soul

        soul = default_soul()
        # Every section a freshly created Agent has on a real 1.17 instance.
        assert set(soul) == {
            "schema_version",
            "prompt",
            "model",
            "tools",
            "knowledge",
            "human",
            "env",
            "memory",
            "sandbox",
            "config_files",
            "config_skills",
            "config_note",
            "app_variables",
            "app_features",
            "misc_legacy",
        }
        assert soul["model"] is None
        assert soul["tools"] == {"cli_tools": [], "dify_tools": []}

    def test_an_instruction_becomes_the_system_prompt(self):
        agent = Agent.create("a", instruction="Be brief.")
        assert agent.soul["prompt"]["system_prompt"] == "Be brief."
        assert agent.instruction == "Be brief."

    def test_the_model_reference_is_split_the_way_dify_records_it(self):
        agent = Agent.create("a", model="langgenius/openai/openai:gpt-4o-mini")
        section = agent.soul["model"]
        assert section["plugin_id"] == "langgenius/openai"
        assert section["model_provider"] == "langgenius/openai/openai"
        assert section["model"] == "gpt-4o-mini"
        assert agent.model == "langgenius/openai/openai:gpt-4o-mini"

    def test_model_settings_are_carried(self):
        agent = Agent.create("a", model="p/q/r:m", temperature=0.2, max_tokens=500)
        assert agent.soul["model"]["model_settings"] == {
            "temperature": 0.2,
            "max_tokens": 500,
        }

    def test_a_bad_model_reference_is_rejected(self):
        with pytest.raises(AgentError, match="model reference"):
            Agent.create("a", model="no-colon-here")

    def test_settings_without_a_model_are_rejected(self):
        with pytest.raises(AgentError, match="without a model"):
            Agent.create("a", temperature=0.2)

    def test_a_tool_is_recorded_with_workspace_identifiers(self):
        from dify_client import dify_tool

        agent = Agent.create("a", tools=[dify_tool(a_tool_spec())])
        tool = agent.soul["tools"]["dify_tools"][0]
        assert tool["tool_name"] == "current_time"
        assert tool["provider"] == "time"
        assert tool["credential_type"] == "unauthorized"

    def test_tools_can_be_added_later(self):
        agent = Agent.create("a")
        agent.add_tool(a_tool_spec())
        assert [t["tool_name"] for t in agent.soul["tools"]["dify_tools"]] == [
            "current_time"
        ]

    def test_the_model_can_be_set_later(self):
        agent = Agent.create("a")
        agent.use_model("p/q/r:m", temperature=0.1)
        assert agent.model == "p/q/r:m"

    def test_a_created_agent_exports_as_an_agent_app(self):
        agent = Agent.create("a", instruction="hi", model="p/q/r:m")
        doc = agent.to_dict()
        assert doc["app"]["mode"] == "agent"
        assert (
            doc["agent_packages"]["agent_1"]["soul"]["prompt"]["system_prompt"] == "hi"
        )


class TestPortableSoul:
    """Dify strips credentials field by field; a blanket sweep breaks import."""

    def test_a_credential_type_survives_as_an_enum_value(self):
        """Nulling it produced `Input should be 'api-key'|'oauth2'|'unauthorized'`."""
        from dify_client import dify_tool

        agent = Agent.create("a", tools=[dify_tool(a_tool_spec())])
        tool = yaml.safe_load(agent.to_yaml())["agent_packages"]["agent_1"]["soul"][
            "tools"
        ]["dify_tools"][0]
        assert tool["credential_type"] == "unauthorized"
        assert tool["credential_ref"] is None

    def test_secret_refs_stay_a_list(self):
        """Nulling it produced `Input should be a valid list`."""
        agent = Agent.create("a")
        agent.soul["env"]["secret_refs"] = [
            {"name": "TOKEN", "key": "k", "type": "env", "value": "sh-should-not-ship"}
        ]
        exported = yaml.safe_load(agent.to_yaml())["agent_packages"]["agent_1"]["soul"]
        assert exported["env"]["secret_refs"] == [
            {"name": "TOKEN", "key": "k", "type": "env"}
        ]

    def test_a_model_credential_is_dropped(self):
        agent = Agent.create("a", model="p/q/r:m")
        agent.soul["model"]["credential_ref"] = "cred-123"
        soul = yaml.safe_load(agent.to_yaml())["agent_packages"]["agent_1"]["soul"]
        assert soul["model"]["credential_ref"] is None
        assert soul["model"]["model"] == "m"

    def test_runtime_parameters_still_get_the_blanket_sweep(self):
        """There the keys are a tool author's, so nothing constrains them."""
        from dify_client import dify_tool

        agent = Agent.create(
            "a", tools=[dify_tool(a_tool_spec(), api_key="sk-LEAK", region="jp")]
        )
        params = yaml.safe_load(agent.to_yaml())["agent_packages"]["agent_1"]["soul"][
            "tools"
        ]["dify_tools"][0]["runtime_parameters"]
        assert params == {"api_key": None, "region": "jp"}

    def test_attached_files_are_marked_missing(self):
        agent = Agent.create("a")
        agent.soul["config_files"] = [{"name": "runbook.md", "file_id": "f-1"}]
        files = yaml.safe_load(agent.to_yaml())["agent_packages"]["agent_1"]["soul"][
            "config_files"
        ]
        assert files == [{"name": "runbook.md", "file_id": "", "is_missing": True}]

    def test_contact_identifiers_are_dropped(self):
        agent = Agent.create("a")
        agent.soul["human"]["contacts"] = [
            {"name": "Ken", "id": "u-1", "tenant_id": "t-1"}
        ]
        contact = yaml.safe_load(agent.to_yaml())["agent_packages"]["agent_1"]["soul"][
            "human"
        ]["contacts"][0]
        assert contact["name"] == "Ken"
        assert contact["id"] is None and contact["tenant_id"] is None

    def test_the_agent_itself_is_untouched(self):
        agent = Agent.create("a", model="p/q/r:m")
        agent.soul["model"]["credential_ref"] = "cred-123"
        agent.to_yaml()
        assert agent.soul["model"]["credential_ref"] == "cred-123"


class TestRoster:
    def test_agents_are_listed_from_the_roster(self):
        """Agents do not appear in the generic app list at all."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/agent")
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "ag-1",
                            "app_id": APP_ID,
                            "name": "triage",
                            "role": "On-call",
                            "description": "d",
                            "active_config_is_published": True,
                        }
                    ]
                },
            )

        http = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://dify.test/console/api",
        )
        console = DifyManagement(
            token=TOKEN, base_url="https://dify.test", http_client=http
        )
        agents = console.agents.list()
        assert agents[0].name == "triage"
        assert agents[0].app_id == APP_ID
        assert agents[0].published is True

    def test_an_unknown_name_is_reported(self):
        def handler(request):
            return httpx.Response(200, json={"data": []})

        http = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://dify.test/console/api",
        )
        console = DifyManagement(
            token=TOKEN, base_url="https://dify.test", http_client=http
        )
        with pytest.raises(ValidationError, match="no Agent named"):
            console.agents.retrieve("nope")
