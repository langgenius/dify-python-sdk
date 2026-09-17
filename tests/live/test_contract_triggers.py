"""Workflows that start themselves, against a real Dify.

A trigger node in a draft is only a drawing: Dify materialises the trigger, and
mints a webhook's URL, on publish. That is the part worth testing on a server.
"""

import uuid

import httpx
import pytest

from dify_client.workflow import Workflow, body_field, header

from .conftest import HARNESS_PREFIX


def webhook_workflow() -> Workflow:
    wf = Workflow(f"{HARNESS_PREFIX}-webhook-{uuid.uuid4().hex[:8]}")
    hook = wf.webhook(
        method="post",
        headers=[header("x-signature", required=True)],
        body=[body_field("order_id", required=True), body_field("total", "number")],
    )
    wf.connect(hook, wf.end({"order_id": hook["order_id"], "total": hook["total"]}))
    return wf


def schedule_workflow() -> Workflow:
    wf = Workflow(f"{HARNESS_PREFIX}-schedule-{uuid.uuid4().hex[:8]}")
    tick = wf.schedule("0 2 * * *", timezone="Asia/Tokyo")
    wf.connect(tick, wf.end({}))
    return wf


@pytest.fixture
def deployed_webhook(needs, management):
    needs("triggers")
    result = management.apps.deploy(webhook_workflow())
    result.raise_for_stage()
    yield result
    management.apps.delete(result.app_id)


class TestWhenTriggersAppear:
    def test_a_draft_has_none(self, needs, management):
        needs("triggers")
        drafted = management.apps.import_definition(webhook_workflow())
        try:
            assert management.apps.triggers.list(drafted.app_id) == []
        finally:
            management.apps.delete(drafted.app_id)

    def test_publishing_materialises_them(self, deployed_webhook, management):
        (trigger,) = management.apps.triggers.list(deployed_webhook.app_id)
        assert trigger.type == "trigger-webhook"
        assert trigger.enabled

    def test_a_schedule_materialises_too(self, needs, management):
        needs("triggers")
        result = management.apps.deploy(schedule_workflow())
        try:
            result.raise_for_stage()
            (trigger,) = management.apps.triggers.list(result.app_id)
            assert trigger.type == "trigger-schedule"
        finally:
            management.apps.delete(result.app_id)


class TestTheWebhookUrl:
    def test_it_is_minted_on_publish(self, deployed_webhook, management):
        hook = management.apps.triggers.webhook(deployed_webhook.app_id)
        assert hook.url.startswith("http")
        assert hook.webhook_id

    def test_posting_to_it_runs_the_workflow(self, deployed_webhook, management):
        hook = management.apps.triggers.webhook(deployed_webhook.app_id)

        reply = httpx.post(
            hook.url,
            headers={"x-signature": "harness", "Content-Type": "application/json"},
            json={"order_id": "A-42", "total": 1980},
            timeout=30,
        )

        assert reply.status_code == 200


class TestPausingATrigger:
    def test_it_can_be_disabled_without_unpublishing(
        self, deployed_webhook, management
    ):
        (trigger,) = management.apps.triggers.list(deployed_webhook.app_id)

        management.apps.triggers.set_enabled(
            deployed_webhook.app_id, trigger, enabled=False
        )
        assert not management.apps.triggers.list(deployed_webhook.app_id)[0].enabled

        management.apps.triggers.set_enabled(deployed_webhook.app_id, trigger)
        assert management.apps.triggers.list(deployed_webhook.app_id)[0].enabled


class TestTriggersDoNotRunLocally:
    def test_the_node_type_is_not_in_graphon(self):
        """Which is why a trigger workflow only runs on a deployed Dify."""
        import pkgutil

        import graphon.nodes as nodes

        installed = {m.name for m in pkgutil.iter_modules(nodes.__path__)}
        assert not any(name.startswith("trigger") for name in installed)
