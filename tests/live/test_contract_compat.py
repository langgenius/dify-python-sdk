"""What this Dify supports, and whether the SDK's assumptions hold on it.

The point of the harness: when a new Dify breaks something, this says which
thing and whether it is a regression or a feature that moved.
"""

import pytest

from dify_client.compat import DSL_VERSION, KNOWN_CAPABILITIES, probe


class TestTheProbe:
    def test_it_answers_for_every_capability_it_knows(self, compat):
        assert set(compat.capabilities) == set(KNOWN_CAPABILITIES)

    def test_it_reports_the_host_it_asked(self, compat, host):
        assert compat.host == host

    def test_nothing_is_undetermined_with_full_credentials(self, compat):
        """Except the ones that genuinely need a credential this harness has
        no reason to hold."""
        assert compat.undetermined() in ([], ["child_chunks"])

    def test_the_summary_says_what_it_found(self, compat):
        summary = compat.summary()
        assert compat.host in summary
        assert DSL_VERSION in summary

    def test_asking_for_an_unknown_capability_lists_the_known_ones(self, compat):
        with pytest.raises(KeyError, match="triggers"):
            compat["telepathy"]

    def test_require_passes_for_what_is_there(self, compat):
        present = [n for n, c in compat.capabilities.items() if c]
        compat.require(*present)

    def test_require_names_what_is_missing(self, compat):
        absent = compat.missing() + compat.undetermined()
        if not absent:
            pytest.skip("this Dify supports everything the SDK asks about")
        from dify_client.exceptions import ValidationError

        with pytest.raises(ValidationError, match=absent[0]):
            compat.require(absent[0])

    def test_probing_without_credentials_still_reaches_the_server(self, host):
        """Enough to tell a reachable Dify from an unreachable one."""
        anonymous = probe(host)
        assert anonymous["console"].available is True
        assert anonymous["agents"].available is None


class TestTheAssumptionsTheSdkMakes:
    def test_the_dsl_version_is_accepted(self, management, fresh_workflow):
        """The SDK writes one version. A Dify that wants a different one asks
        for confirmation rather than failing, and this catches the drift."""
        result = management.apps.import_definition(fresh_workflow())
        try:
            assert result.imported, result.error
        finally:
            if result.app_id:
                management.apps.delete(result.app_id)

    def test_a_workflow_app_is_reported_as_workflow_mode(self, workflow_deployment):
        assert workflow_deployment.app_mode == "workflow"

    def test_an_answer_node_makes_it_a_chatflow(self, chatflow_deployment):
        """The SDK infers the mode from the graph; Dify must agree."""
        assert chatflow_deployment.app_mode == "advanced-chat"

    def test_the_console_needs_a_csrf_token_for_writes(self, compat):
        """Since 1.17. Recorded so a change either way is noticed."""
        assert compat["console_csrf"].known


class TestVersionReporting:
    def test_the_index_reports_the_running_version(self, service_api):
        """`GET /v1/` — no credential, always served. The two endpoints that
        look like they would answer do not: `/openapi/v1/_version` needs
        OPENAPI_ENABLED, and `/console/api/version` reports the latest
        *released* version rather than this one."""
        from dify_client import DifyApp

        info = DifyApp.probe(service_api)
        assert info.server_version
        assert info.api_version == "v1"

    def test_the_probe_uses_it(self, compat, service_api):
        from dify_client import DifyApp

        assert compat.version == DifyApp.probe(service_api).server_version

    def test_a_client_reports_the_same_thing(self, workflow_app):
        assert workflow_app.server_info().server_version

    def test_the_console_version_endpoint_is_not_this_one(self, host):
        """It answers with the latest release, which is why nothing uses it."""
        import httpx

        reply = httpx.get(
            f"{host}/console/api/version", params={"current_version": "0.0.0"}
        )
        if reply.status_code != 200:
            pytest.skip("this Dify does not serve /console/api/version")
        from dify_client import DifyApp

        assert reply.json().get("version") != DifyApp.probe(f"{host}/v1").server_version

    def test_an_absent_version_is_not_mistaken_for_an_old_one(self, compat):
        """Empty means "not reported", and the summary says so."""
        if compat.version:
            pytest.skip("this Dify reports its version")
        assert "not reported" in compat.summary()
