"""Asking a Dify what it supports, without one in front of you.

The probe is a library feature, not just harness scaffolding: the same
pre-flight is what stops a workflow with a trigger node from being deployed to
a Dify that has no triggers, where it would import cleanly and never run.
"""

import httpx
import pytest

from dify_client.compat import (
    DSL_VERSION,
    KNOWN_CAPABILITIES,
    Capability,
    Compatibility,
    probe,
)
from dify_client.exceptions import ValidationError


def dify(**routes):
    def handler(request):
        for path, reply in routes.items():
            if request.url.path == path:
                return reply
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


def probe_with(transport, host="https://dify.test", **kwargs):
    import dify_client.compat as module

    original = httpx.Client

    class Patched(original):
        def __init__(self, *args, **init):
            init["transport"] = transport
            super().__init__(*args, **init)

    module.httpx.Client = Patched
    try:
        return probe(host, **kwargs)
    finally:
        module.httpx.Client = original


VERSIONED = dify(
    **{
        "/openapi/v1/_version": httpx.Response(
            200, json={"version": "1.17.1", "edition": "COMMUNITY"}
        ),
        "/console/api/setup": httpx.Response(200, json={"step": "finished"}),
    }
)


class TestWhatItReports:
    def test_the_version_comes_from_openapi(self):
        found = probe_with(VERSIONED)
        assert found.version == "1.17.1"
        assert found.edition == "COMMUNITY"

    def test_openapi_being_off_is_an_answer_not_a_failure(self):
        found = probe_with(
            dify(
                **{
                    "/console/api/setup": httpx.Response(200, json={}),
                }
            )
        )
        assert found["openapi"].available is False
        assert "OPENAPI_ENABLED" in found["openapi"].detail

    def test_an_unreported_version_is_empty_not_guessed(self):
        """Empty means "not reported", never "old"."""
        found = probe_with(dify(**{"/console/api/setup": httpx.Response(200, json={})}))
        assert found.version == ""
        assert "not reported" in found.summary()

    def test_it_says_which_dsl_version_the_sdk_writes(self):
        assert probe_with(VERSIONED).dsl_version == DSL_VERSION

    def test_it_asks_about_everything_it_knows(self):
        assert set(probe_with(VERSIONED).capabilities) == set(KNOWN_CAPABILITIES)


class TestThreeAnswersNotTwo:
    """Available, absent, and not-determined are different things."""

    def test_without_credentials_the_console_side_is_undetermined(self):
        found = probe_with(VERSIONED)
        assert found["agents"].available is None
        assert "management" in found["agents"].detail

    def test_without_an_app_the_service_side_is_undetermined(self):
        found = probe_with(VERSIONED)
        assert found["human_input"].available is None
        assert "app" in found["human_input"].detail

    def test_undetermined_is_not_treated_as_supported(self):
        found = probe_with(VERSIONED)
        assert not found.supports("agents")

    def test_undetermined_is_listed_separately_from_missing(self):
        found = probe_with(VERSIONED)
        assert "agents" in found.undetermined()
        assert "agents" not in found.missing()

    def test_an_unreachable_host_is_undetermined_rather_than_absent(self):
        def refuse(request):
            raise httpx.ConnectError("refused")

        found = probe_with(httpx.MockTransport(refuse))
        assert found["openapi"].available is None
        assert "could not reach" in found["openapi"].detail


class TestRequire:
    def _compat(self, **states):
        return Compatibility(
            host="https://dify.test",
            capabilities={
                name: Capability(name, state, f"{name} detail")
                for name, state in states.items()
            },
        )

    def test_it_passes_when_everything_is_there(self):
        compat = self._compat(triggers=True, agents=True)
        assert compat.require("triggers", "agents") is compat

    def test_it_names_what_is_missing_and_why(self):
        compat = self._compat(triggers=False)
        with pytest.raises(ValidationError) as caught:
            compat.require("triggers")
        assert "triggers" in str(caught.value)
        assert "triggers detail" in str(caught.value)
        assert "https://dify.test" in str(caught.value)

    def test_undetermined_fails_the_requirement_too(self):
        """Not knowing is not permission to proceed."""
        with pytest.raises(ValidationError):
            self._compat(triggers=None).require("triggers")

    def test_it_reports_every_missing_one_at_once(self):
        compat = self._compat(triggers=False, agents=False, skills=True)
        with pytest.raises(ValidationError) as caught:
            compat.require("triggers", "agents", "skills")
        assert "triggers" in str(caught.value)
        assert "agents" in str(caught.value)


class TestTheApi:
    def test_a_capability_is_truthy_only_when_available(self):
        assert bool(Capability("x", True))
        assert not bool(Capability("x", False))
        assert not bool(Capability("x", None))

    def test_an_unknown_name_lists_the_known_ones(self):
        compat = Compatibility(host="h", capabilities={})
        with pytest.raises(KeyError, match="openapi"):
            compat["telepathy"]

    def test_every_known_capability_is_documented(self):
        assert all(KNOWN_CAPABILITIES.values())

    def test_the_summary_is_readable(self):
        summary = probe_with(VERSIONED).summary()
        assert "Dify at https://dify.test" in summary
        assert "1.17.1" in summary
