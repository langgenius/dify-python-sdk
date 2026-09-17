"""Trigger nodes, and the console endpoints that manage them once published."""

import json

import httpx
import pytest

from dify_client import DifyManagement
from dify_client.console import Trigger
from dify_client.workflow import (
    Workflow,
    WorkflowError,
    body_field,
    header,
    query_param,
)

APP = "68f0d6a0-8367-4b54-b730-7e5e424c57be"


class Dify:
    """A console stand-in that answers one route and records the request."""

    def __init__(self):
        self.replies: dict[tuple[str, str], dict] = {}
        self.last_params: dict = {}
        self.last_body: dict | None = None

    def reply(self, method: str, path: str, body: dict) -> None:
        self.replies[(method, path)] = body

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/console/api", "")
        self.last_params = dict(request.url.params)
        self.last_body = json.loads(request.content) if request.content else None
        body = self.replies.get((request.method, path))
        if body is None:
            return httpx.Response(
                404, json={"message": f"no stub for {request.method} {path}"}
            )
        return httpx.Response(200, json=body)


@pytest.fixture
def dify():
    return Dify()


@pytest.fixture
def console(dify):
    http = httpx.Client(
        transport=httpx.MockTransport(dify.handler),
        base_url="https://dify.test/console/api",
    )
    return DifyManagement(
        token="ey.FAKE.CONSOLE.TOKEN", base_url="https://dify.test", http_client=http
    )


def _node(wf: Workflow, node_id: str) -> dict:
    (found,) = [
        n for n in wf.to_dict()["workflow"]["graph"]["nodes"] if n["id"] == node_id
    ]
    return found["data"]


class TestWebhookNode:
    def test_it_starts_a_workflow_without_a_start_node(self):
        wf = Workflow("orders")
        hook = wf.webhook(body=[body_field("order_id")])
        wf.connect(hook, wf.end({"id": hook["order_id"]}))

        assert _node(wf, "trigger_webhook")["type"] == "trigger-webhook"

    def test_a_declared_field_becomes_an_output_reference(self):
        wf = Workflow("orders")
        hook = wf.webhook(body=[body_field("order_id")])
        assert hook["order_id"].template == "{{#trigger_webhook.order_id#}}"

    def test_the_method_is_lowercased_the_way_dify_stores_it(self):
        wf = Workflow("orders")
        wf.webhook(method="POST")
        wf.connect(wf.nodes[0], wf.end())
        assert _node(wf, "trigger_webhook")["method"] == "post"

    def test_each_surface_keeps_its_own_fields(self):
        wf = Workflow("orders")
        wf.webhook(
            headers=[header("x-signature")],
            params=[query_param("page", "number")],
            body=[body_field("payload", "object")],
        )
        wf.connect(wf.nodes[0], wf.end())
        data = _node(wf, "trigger_webhook")
        assert [p["name"] for p in data["headers"]] == ["x-signature"]
        assert [p["name"] for p in data["params"]] == ["page"]
        assert [p["name"] for p in data["body"]] == ["payload"]

    @pytest.mark.parametrize(
        ("factory", "type"),
        [(query_param, "object"), (query_param, "file"), (body_field, "unknown")],
    )
    def test_a_type_the_surface_does_not_allow_is_refused(self, factory, type):
        with pytest.raises(ValueError, match="Dify allows"):
            factory("f", type)

    def test_a_header_is_a_string_and_takes_no_type_to_get_wrong(self):
        assert header("x-token").type == "string"


class TestScheduleNode:
    def test_a_cron_expression_is_stored_as_cron_mode(self):
        wf = Workflow("nightly")
        wf.connect(wf.schedule("0 2 * * *", timezone="Asia/Tokyo"), wf.end())
        data = _node(wf, "trigger_schedule")
        assert data["mode"] == "cron"
        assert data["cron_expression"] == "0 2 * * *"
        assert data["timezone"] == "Asia/Tokyo"

    def test_a_frequency_is_stored_as_visual_mode(self):
        wf = Workflow("weekly")
        wf.connect(
            wf.schedule(
                frequency="weekly", at={"time": "9:00 AM", "weekdays": ["mon"]}
            ),
            wf.end(),
        )
        data = _node(wf, "trigger_schedule")
        assert data["mode"] == "visual"
        assert data["frequency"] == "weekly"
        assert data["visual_config"]["weekdays"] == ["mon"]

    def test_neither_cron_nor_frequency_is_refused(self):
        with pytest.raises(WorkflowError, match="exactly one"):
            Workflow("w").schedule()

    def test_both_at_once_is_refused(self):
        with pytest.raises(WorkflowError, match="exactly one"):
            Workflow("w").schedule("0 2 * * *", frequency="daily")

    def test_a_frequency_dify_does_not_know_is_refused(self):
        with pytest.raises(WorkflowError, match="fortnightly"):
            Workflow("w").schedule(frequency="fortnightly")

    def test_a_cron_expression_with_visual_detail_is_refused(self):
        """at= describes a frequency; cron already says when."""
        with pytest.raises(WorkflowError, match="already says when"):
            Workflow("w").schedule("0 2 * * *", at={"time": "9:00 AM"})


class TestValidation:
    def test_a_trigger_satisfies_the_root_requirement(self):
        wf = Workflow("orders")
        wf.connect(wf.webhook(), wf.end())
        wf.validate()

    def test_a_workflow_with_neither_is_still_refused(self):
        wf = Workflow("orders")
        wf.connect(wf.template("a"), wf.end())
        with pytest.raises(WorkflowError, match="nothing to start from"):
            wf.validate()

    def test_a_trigger_workflow_still_needs_a_terminal_node(self):
        wf = Workflow("orders")
        hook = wf.webhook()
        wf.connect(hook, wf.template("a"))
        with pytest.raises(WorkflowError, match="'end' node"):
            wf.validate()


class TestConsoleTriggers:
    def test_listing_reads_the_apps_triggers(self, console, dify):
        dify.reply(
            "GET",
            f"/apps/{APP}/triggers",
            {
                "data": [
                    {
                        "id": "t1",
                        "trigger_type": "trigger-webhook",
                        "title": "Webhook",
                        "node_id": "trigger_webhook",
                        "provider_name": "",
                        "icon": "",
                        "status": "enabled",
                    }
                ]
            },
        )

        (trigger,) = console.apps.triggers.list(APP)

        assert trigger == Trigger(
            id="t1",
            type="trigger-webhook",
            title="Webhook",
            node_id="trigger_webhook",
            status="enabled",
            provider_name="",
        )
        assert trigger.enabled

    def test_a_disabled_trigger_reads_as_disabled(self, console, dify):
        dify.reply(
            "GET",
            f"/apps/{APP}/triggers",
            {
                "data": [
                    {
                        "id": "t1",
                        "trigger_type": "trigger-schedule",
                        "title": "Nightly",
                        "node_id": "trigger_schedule",
                        "provider_name": "",
                        "icon": "",
                        "status": "disabled",
                    }
                ]
            },
        )

        assert not console.apps.triggers.list(APP)[0].enabled

    def test_the_webhook_url_is_looked_up_by_node_id(self, console, dify):
        dify.reply(
            "GET",
            f"/apps/{APP}/workflows/triggers/webhook",
            {
                "id": "w1",
                "webhook_id": "wh-1",
                "node_id": "trigger_webhook",
                "webhook_url": "https://dify.test/webhook/wh-1",
                "webhook_debug_url": "https://dify.test/webhook/wh-1/debug",
            },
        )

        hook = console.apps.triggers.webhook(APP, "trigger_webhook")

        assert hook.url == "https://dify.test/webhook/wh-1"
        assert hook.debug_url.endswith("/debug")
        assert dify.last_params == {"node_id": "trigger_webhook"}

    def test_disabling_sends_the_flag_dify_expects(self, console, dify):
        dify.reply(
            "POST",
            f"/apps/{APP}/trigger-enable",
            {
                "id": "t1",
                "trigger_type": "trigger-schedule",
                "title": "Nightly",
                "node_id": "trigger_schedule",
                "provider_name": "",
                "icon": "",
                "status": "disabled",
            },
        )

        result = console.apps.triggers.set_enabled(APP, "t1", enabled=False)

        assert dify.last_body == {"trigger_id": "t1", "enable_trigger": False}
        assert not result.enabled

    def test_enabling_is_the_default(self, console, dify):
        dify.reply(
            "POST",
            f"/apps/{APP}/trigger-enable",
            {
                "id": "t1",
                "trigger_type": "trigger-schedule",
                "title": "N",
                "node_id": "n",
                "provider_name": "",
                "icon": "",
                "status": "enabled",
            },
        )

        console.apps.triggers.set_enabled(APP, "t1")

        assert dify.last_body["enable_trigger"] is True
