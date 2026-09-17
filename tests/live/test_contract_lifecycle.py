"""Getting a code-defined app onto Dify, and off it again.

The steps Dify keeps apart — import, publish, run — and the states in between,
checked against the server rather than against a mock of it.
"""

import pytest

from dify_client import Stage
from dify_client.exceptions import APIError


class TestTheSteps:
    def test_importing_stops_at_a_draft(self, management, fresh_workflow):
        result = management.apps.import_definition(fresh_workflow())
        try:
            assert result.stage is Stage.DRAFTED
            assert result.imported
            assert not result.published
            assert result.created
        finally:
            management.apps.delete(result.app_id)

    def test_a_draft_does_not_run(self, management, fresh_workflow, service_api):
        """The Service API runs the published version. This is why importing
        and running, without publishing, tests the wrong thing."""
        from dify_client import DifyApp

        result = management.apps.import_definition(fresh_workflow())
        try:
            key = management.apps.keys.create(result.app_id).token
            app = DifyApp(key, base_url=service_api, user="sdk-harness")
            with pytest.raises(APIError):
                app.workflows.runs.create({"word": "hello"})
        finally:
            management.apps.delete(result.app_id)

    def test_publishing_makes_it_run(self, management, fresh_workflow, service_api):
        from dify_client import DifyApp

        result = management.apps.import_definition(fresh_workflow())
        try:
            management.apps.publish(result.app_id)
            key = management.apps.keys.create(result.app_id).token
            app = DifyApp(key, base_url=service_api, user="sdk-harness")
            assert app.workflows.runs.create({"word": "hello"}).succeeded
        finally:
            management.apps.delete(result.app_id)

    def test_deploy_does_all_three(self, management, fresh_workflow):
        result = management.apps.deploy(fresh_workflow())
        try:
            assert result.stage is Stage.RUNNABLE
            assert result.api_key
            assert result.app_mode == "workflow"
        finally:
            management.apps.delete(result.app_id)


class TestOverwriting:
    def test_a_redeploy_replaces_what_runs(
        self, management, fresh_workflow, service_api
    ):
        """The published version must become the new one, not stay the old."""
        from dify_client import DifyApp
        from dify_client.workflow import Workflow, text_input

        first = management.apps.deploy(fresh_workflow())
        try:
            app = DifyApp(first.api_key, base_url=service_api, user="sdk-harness")
            assert app.workflows.runs.create({"word": "a"}).outputs["out"] == "a!"

            changed = Workflow(management.apps.retrieve(first.app_id).name)
            start = changed.start([text_input("word")])
            quiet = changed.template(
                "{{ word }}...", variables={"word": start["word"]}, id="shout"
            )
            changed.connect(start, quiet, changed.end({"out": quiet.output}))

            management.apps.deploy(
                changed, app_id=first.app_id, api_key=first.api_key
            ).raise_for_stage(Stage.PUBLISHED)

            assert app.workflows.runs.create({"word": "a"}).outputs["out"] == "a..."
        finally:
            management.apps.delete(first.app_id)

    def test_an_existing_app_is_not_marked_created(self, management, fresh_workflow):
        first = management.apps.deploy(fresh_workflow())
        try:
            again = management.apps.deploy(
                fresh_workflow(), app_id=first.app_id, api_key=first.api_key
            )
            assert not again.created
        finally:
            management.apps.delete(first.app_id)


class TestTemporaryApps:
    def test_it_is_gone_afterwards(self, management, fresh_workflow):
        with management.apps.temporary(fresh_workflow()) as app:
            app_id = app.id
            assert app.client().workflows.runs.create({"word": "x"}).succeeded

        remaining = {a.id for a in management.apps.list(limit=100)}
        assert app_id not in remaining

    def test_it_is_gone_after_a_failure(self, management, fresh_workflow):
        app_id = None
        with pytest.raises(RuntimeError):
            with management.apps.temporary(fresh_workflow()) as app:
                app_id = app.id
                raise RuntimeError("the test failed")

        remaining = {a.id for a in management.apps.list(limit=100)}
        assert app_id not in remaining


class TestKeys:
    def test_a_key_is_listed_after_minting(self, management, fresh_workflow):
        result = management.apps.deploy(fresh_workflow())
        try:
            keys = management.apps.keys.list(result.app_id)
            assert keys
        finally:
            management.apps.delete(result.app_id)

    def test_a_key_can_be_revoked(self, management, fresh_workflow):
        result = management.apps.deploy(fresh_workflow())
        try:
            extra = management.apps.keys.create(result.app_id)
            management.apps.keys.delete(result.app_id, extra.id)
            remaining = {k.id for k in management.apps.keys.list(result.app_id)}
            assert extra.id not in remaining
        finally:
            management.apps.delete(result.app_id)


class TestExport:
    def test_the_dsl_round_trips(self, management, fresh_workflow):
        """What comes back must import again — the property that makes an
        export safe to keep in version control."""
        import yaml

        result = management.apps.deploy(fresh_workflow())
        try:
            exported = management.apps.export(result.app_id)
            document = yaml.safe_load(exported)
            assert document["kind"] == "app"
            assert document["workflow"]["graph"]["nodes"]

            reimported = management.apps.import_definition(exported)
            assert reimported.imported
            management.apps.delete(reimported.app_id)
        finally:
            management.apps.delete(result.app_id)

    def test_secrets_are_blanked_by_default(self, management, fresh_workflow):
        result = management.apps.deploy(fresh_workflow())
        try:
            assert "include_secret" not in management.apps.export(result.app_id)
        finally:
            management.apps.delete(result.app_id)


class TestFindingApps:
    def test_a_deployed_app_appears_in_the_listing(
        self, management, workflow_deployment
    ):
        ids = {a.id for a in management.apps.list(limit=100)}
        assert workflow_deployment.app_id in ids

    def test_it_can_be_found_by_name(self, management, workflow_deployment):
        name = management.apps.retrieve(workflow_deployment.app_id).name
        assert management.apps.retrieve(name).id == workflow_deployment.app_id

    def test_the_mode_filter_narrows(self, management, workflow_deployment):
        workflows = management.apps.list(mode="workflow", limit=100)
        assert all(a.mode == "workflow" for a in workflows)
        assert workflow_deployment.app_id in {a.id for a in workflows}

    def test_opening_one_gives_a_usable_client(self, management, workflow_deployment):
        name = management.apps.retrieve(workflow_deployment.app_id).name
        managed = management.apps.open(name)
        try:
            assert managed.client().workflows.runs.create({"word": "x"}).succeeded
        finally:
            management.apps.keys.delete(
                managed.id,
                next(
                    k.id
                    for k in management.apps.keys.list(managed.id)
                    if k.token.startswith(managed.api_key[:12])
                ),
            )


class TestDeployingAnAgent:
    """An Agent is a different app mode with a different publish, and the SDK
    treated it as "nothing to publish". It imports, then answers every key
    request with "Publish the Agent before enabling Web App or API access" —
    so `deploy()` reported published-but-keyless and nobody could run it.

    Free: the app is created, published and deleted without a model call.
    """

    SOUL = {
        "prompt": {"system_prompt": "Triage inbound support messages."},
        "model": {
            "plugin_id": "langgenius/openai",
            "model_provider": "langgenius/openai/openai",
            "model": "gpt-4o-mini",
            "model_settings": {"temperature": 0.7},
        },
    }

    def _agent(self):
        import uuid

        from dify_client.agent import Agent

        return Agent(
            f"sdk-harness-agent-{uuid.uuid4().hex[:8]}",
            soul=self.SOUL,
            description="Fixture Agent for the SDK harness.",
            role="Support engineer",
        )

    def _deploy(self, management, definition):
        result = management.apps.deploy(definition)
        if not result.imported:
            pytest.skip(f"this Dify would not import an Agent: {result.error}")
        return result

    def test_an_agent_object_reaches_a_runnable_app(self, management):
        result = self._deploy(management, self._agent())
        try:
            assert result.app_mode == "agent"
            assert result.published, result.error
            assert result.has_key, result.error
            assert result.stage is Stage.RUNNABLE
        finally:
            management.apps.delete(result.app_id)

    def test_the_same_agent_as_dsl_reaches_the_same_place(self, management):
        """`deploy()` takes DSL as a string, and a string has no `.mode` — the
        one that decided whether this was an Agent at all."""
        result = self._deploy(management, self._agent().to_yaml())
        try:
            assert result.app_mode == "agent"
            assert result.published, result.error
            assert result.has_key, result.error
        finally:
            management.apps.delete(result.app_id)

    def test_publishing_it_as_a_workflow_is_refused_by_dify(self, management):
        """Why the mode has to be read: this is what the SDK used to call."""
        result = self._deploy(management, self._agent())
        try:
            with pytest.raises(APIError):
                management.apps.publish(result.app_id)
        finally:
            management.apps.delete(result.app_id)
