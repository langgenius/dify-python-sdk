"""The harness: one real Dify, shared fixtures, nothing left behind.

These tests talk to a running Dify. They are skipped unless one is configured,
so `pytest` on a laptop with no server still passes.

    export DIFY_HOST=http://localhost
    export DIFY_CONSOLE_EMAIL=… DIFY_CONSOLE_PASSWORD=…
    pytest tests/live -m live

What each test needs is asked of the server rather than assumed: a Dify without
triggers skips the trigger tests instead of failing them, and says which
capability was missing. See :mod:`dify_client.compat`.

Apps this harness creates are named with `HARNESS_PREFIX` and deleted at the
end of the session, including the ones a failing test left behind.
"""

from __future__ import annotations

import os
import uuid

import pytest

from dify_client import (
    AsyncDifyApp,
    AsyncDifyKnowledge,
    DifyApp,
    DifyKnowledge,
    DifyManagement,
)
from dify_client.compat import Compatibility, probe
from dify_client.workflow import Workflow, paragraph, text_input

#: Every app this harness creates starts with this, so cleanup can find them
#: even after a crash, and a human can tell them from real work.
HARNESS_PREFIX = "sdk-harness"

#: Knowledge bases this harness creates start with this.
HARNESS_DATASET = f"{HARNESS_PREFIX}-dataset"

#: Set to run against a real Dify. Without it the whole directory skips.
HOST_ENV = "DIFY_HOST"
EMAIL_ENV = "DIFY_CONSOLE_EMAIL"
PASSWORD_ENV = "DIFY_CONSOLE_PASSWORD"


def _configured() -> str | None:
    """Why the harness cannot run, or None if it can."""
    missing = [n for n in (HOST_ENV, EMAIL_ENV, PASSWORD_ENV) if not os.environ.get(n)]
    if missing:
        return f"no Dify configured: set {', '.join(missing)}"
    return None


def pytest_collection_modifyitems(config, items):
    """Mark everything in this directory `live`, and skip it when unconfigured."""
    reason = _configured()
    for item in items:
        if "tests/live" not in str(item.fspath).replace(os.sep, "/"):
            continue
        item.add_marker(pytest.mark.live)
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session")
def host() -> str:
    return os.environ[HOST_ENV].rstrip("/")


@pytest.fixture(scope="session")
def service_api(host: str) -> str:
    return f"{host}/v1"


@pytest.fixture(scope="session")
def management(host: str) -> DifyManagement:
    """One console session for the whole run.

    Logs in rather than taking a token: a console token is short-lived, and a
    stale one in the environment is the most common way this harness fails for
    a reason that has nothing to do with the SDK.
    """
    console = DifyManagement.login(
        os.environ[EMAIL_ENV], os.environ[PASSWORD_ENV], base_url=host
    )
    yield console
    console.close()


@pytest.fixture(scope="session")
def compat(management: DifyManagement, workflow_app: DifyApp) -> Compatibility:
    """What this Dify supports. Printed once, at the top of the run."""
    found = probe(management=management, app=workflow_app)
    print("\n" + found.summary())
    return found


@pytest.fixture
def needs(compat: Compatibility):
    """Skip a test whose capability this Dify lacks.

    An absent feature is not a failing SDK, and reporting it as one wastes the
    reader's time::

        def test_webhooks(needs, management):
            needs("triggers")
    """

    def require(*names: str) -> None:
        for name in names:
            capability = compat[name]
            if not capability:
                pytest.skip(f"this Dify lacks {name} — {capability.detail or 'absent'}")

    return require


# -- fixture apps ---------------------------------------------------------
#
# Two apps, deployed once per session: one workflow, one chatflow. Most tests
# only read from them, so paying to create them per test would be waste.


def _unique(name: str) -> str:
    return f"{HARNESS_PREFIX}-{name}-{uuid.uuid4().hex[:8]}"


def _workflow() -> Workflow:
    wf = Workflow(_unique("workflow"), description="Fixture app for the SDK harness.")
    start = wf.start([text_input("word"), paragraph("note", required=False)])
    shout = wf.template(
        "{{ word }}!", variables={"word": start["word"]}, title="Shout", id="shout"
    )
    wf.connect(start, shout, wf.end({"out": shout.output, "echo": start["word"]}))
    return wf


def _chatflow() -> Workflow:
    wf = Workflow(_unique("chatflow"), description="Fixture app for the SDK harness.")
    start = wf.start([text_input("topic")])
    reply = wf.template(
        "about {{ topic }}", variables={"topic": start["topic"]}, id="reply"
    )
    wf.connect(start, reply, wf.answer(reply.output))
    return wf


def _deploy(management: DifyManagement, definition: Workflow):
    result = management.apps.deploy(definition)
    result.raise_for_stage()
    return result


@pytest.fixture(scope="session")
def workflow_deployment(management: DifyManagement):
    """A deployed `workflow` app, shared by the session."""
    result = _deploy(management, _workflow())
    yield result
    management.apps.delete(result.app_id)


@pytest.fixture(scope="session")
def chatflow_deployment(management: DifyManagement):
    """A deployed `advanced-chat` app, shared by the session."""
    result = _deploy(management, _chatflow())
    yield result
    management.apps.delete(result.app_id)


@pytest.fixture(scope="session")
def workflow_app(workflow_deployment, service_api: str) -> DifyApp:
    app = DifyApp(workflow_deployment.api_key, base_url=service_api, user="sdk-harness")
    yield app
    app.close()


@pytest.fixture(scope="session")
def chat_app(chatflow_deployment, service_api: str) -> DifyApp:
    app = DifyApp(chatflow_deployment.api_key, base_url=service_api, user="sdk-harness")
    yield app
    app.close()


@pytest.fixture
def fresh_workflow():
    """A workflow definition no other test shares, for deploy-cycle tests."""
    return _workflow


@pytest.fixture(scope="session", autouse=True)
def _sweep(request):
    """Delete anything this harness left behind, including from earlier runs.

    A crash mid-session leaves an app in the workspace. Rather than ask a human
    to notice, the next run cleans up after the last one.
    """
    yield
    if _configured():
        return
    console = DifyManagement.login(
        os.environ[EMAIL_ENV],
        os.environ[PASSWORD_ENV],
        base_url=os.environ[HOST_ENV].rstrip("/"),
    )
    try:
        stale = [
            a for a in console.apps.list(limit=100) if a.name.startswith(HARNESS_PREFIX)
        ]
        for app in stale:
            console.apps.delete(app.id)
        if stale:
            print(f"\nharness: swept {len(stale)} leftover app(s)")
    finally:
        console.close()


@pytest.fixture(scope="session")
def knowledge(management, service_api):
    """A knowledge client, keyed by a dataset key minted for the run.

    Minted here and revoked at the end, because a dataset key is reveal-once
    and Dify caps a workspace at ten of them. A run that mints one and walks
    away burns a tenth of the cap every time; ten runs later minting fails and
    nothing in the error says why. There is no falling back to a key already in
    the workspace either — the listing masks the token, and a masked token
    authenticates as "Access token is invalid", which reads like a bug in the
    SDK rather than like the cap.
    """
    reply = management._client.post("/datasets/api-keys", headers=management._headers())
    if reply.status_code >= 400:
        pytest.skip(
            "could not mint a dataset API key "
            f"({reply.status_code}: {reply.text[:120]}). A workspace holds ten; "
            "delete some in the console, or see tests/live/README.md."
        )
    minted = reply.json()

    client = DifyKnowledge(minted["token"], base_url=service_api)
    yield client
    client.close()
    management._client.delete(
        f"/datasets/api-keys/{minted['id']}", headers=management._headers()
    )


@pytest.fixture
def dataset(knowledge):
    """A knowledge base for one test, deleted after."""
    import uuid

    created = knowledge.datasets.create(f"{HARNESS_DATASET}-{uuid.uuid4().hex[:8]}")
    yield created
    knowledge.datasets.delete(created)


@pytest.fixture
async def async_knowledge(knowledge, service_api):
    """The async knowledge client, on the same minted key as the sync one."""
    client = AsyncDifyKnowledge(knowledge.api_key, base_url=service_api)
    yield client
    await client.aclose()


@pytest.fixture
async def async_workflow_app(workflow_deployment, service_api):
    app = AsyncDifyApp(
        workflow_deployment.api_key, base_url=service_api, user=HARNESS_PREFIX
    )
    yield app
    await app.aclose()


@pytest.fixture
async def async_chat_app(chatflow_deployment, service_api):
    app = AsyncDifyApp(
        chatflow_deployment.api_key, base_url=service_api, user=HARNESS_PREFIX
    )
    yield app
    await app.aclose()
