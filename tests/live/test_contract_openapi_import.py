"""Importing through ``/openapi/v1``, read the way the console path reads it.

It raised over a held import — "Dify is asking" read as "Dify said no" — and a
string of DSL never reached Dify at all. The bearer is minted from the
harness's console session for the one test and revoked on the way out, so no
device session is left behind.
"""

import uuid

import pytest

from dify_client import OpenApiClient
from dify_client.workflow import DSL_VERSION, Workflow, text_input

from .conftest import HARNESS_PREFIX


@pytest.fixture
def openapi(management, host):
    try:
        token = management.mint_openapi_token(
            client_id="difyctl", device_label=f"{HARNESS_PREFIX}-openapi"
        )
    except Exception as refusal:  # noqa: BLE001 - absence is not failure here
        pytest.skip(f"this Dify mints no /openapi/v1 token: {refusal}")
    client = OpenApiClient(token=token, base_url=host)
    yield client
    client._client.delete("/account/sessions/self", headers=client._headers())


def _greeter() -> Workflow:
    wf = Workflow(f"{HARNESS_PREFIX}-openapi-{uuid.uuid4().hex[:6]}")
    start = wf.start([text_input("q")])
    wf.end({"o": start["q"]})
    return wf


def test_a_string_of_dsl_is_imported(openapi, management):
    result = openapi.apps.import_definition(_greeter().to_yaml())
    try:
        assert result.imported is True and result.app_id
    finally:
        management.apps.delete(result.app_id)


def test_a_held_import_is_held_and_confirming_completes_it(openapi, management):
    """A document newer than the server is held rather than refused."""
    text = _greeter().to_yaml().replace(f"version: {DSL_VERSION}", "version: 0.99.0")
    assert "version: 0.99.0" in text

    held = openapi.apps.import_definition(text)
    assert held.imported is False
    assert held.needs_confirmation is True
    assert held.import_id

    done = openapi.apps.confirm(held)
    try:
        assert done.imported is True and done.app_id
    finally:
        management.apps.delete(done.app_id)
