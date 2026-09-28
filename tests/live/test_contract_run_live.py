"""``run_live``: a definition run on Dify as a draft, with nothing released.

It used to deploy over a named app and publish, so testing a change replaced
what that app's users were served. These pin what it does now — import into a
temporary app, run the draft the way the editor's Run button does, delete it —
against a real Dify, including that a named app's published version is left
alone.

The free tests here call no model, but ``run_live`` asks for
``DIFY_LIVE_TESTS`` regardless — it cannot tell a template from an LLM — so
they set it themselves. The one that does spend is marked billed.
"""

import uuid

import pytest

from dify_client import DifyApp
from dify_client.workflow import BudgetExceeded, Workflow, text_input
from dify_client.workflow.live import LIVE_ENABLED_ENV

from .conftest import HARNESS_PREFIX, billed


def _named(what: str) -> str:
    return f"{HARNESS_PREFIX}-{what}-{uuid.uuid4().hex[:6]}"


def _greeter(name: str, text: str) -> Workflow:
    wf = Workflow(name)
    start = wf.start([text_input("q")])
    wf.end(
        {"o": wf.template(f"{text} {{{{ q }}}}", variables={"q": start["q"]}).output}
    )
    return wf


@pytest.fixture
def free_to_run(monkeypatch):
    """These call no model; the flag is set because run_live cannot know that."""
    monkeypatch.setenv(LIVE_ENABLED_ENV, "1")


def _app_ids(management) -> set[str]:
    return {app.id for app in management.apps.list()}


def test_a_workflow_runs_as_a_draft_and_leaves_nothing(management, free_to_run):
    before = _app_ids(management)

    result = _greeter(_named("rl-workflow"), "Hi").run_live(
        {"q": "draft"}, console=management
    )

    assert result.status == "succeeded"
    assert result.outputs == {"o": "Hi draft"}
    assert _app_ids(management) == before


def test_a_chatflow_runs_as_a_draft_on_its_own_route(management, free_to_run):
    """Dify serves a chatflow's draft at /advanced-chat/..., with a message."""
    wf = Workflow(_named("rl-chatflow"))
    start = wf.start([text_input("q")])
    wf.answer(wf.template("You said {{ m }}", variables={"m": start["q"]}))

    result = wf.run_live({"q": "x"}, console=management, query="hello")

    assert result.status == "succeeded"
    assert result.outputs["answer"] == "You said x"


def test_a_named_app_s_published_version_is_left_alone(
    management, service_api, free_to_run
):
    """Its draft is overwritten and run; what the Service API serves is not."""
    released = _greeter(_named("rl-named"), "OLD")
    deployed = management.apps.deploy(released)
    try:
        deployed.raise_for_stage()
        changed = _greeter(released.name, "NEW")

        result = changed.run_live(
            {"q": "v"}, console=management, app_id=deployed.app_id
        )

        assert result.outputs == {"o": "NEW v"}
        with DifyApp(
            deployed.api_key, base_url=service_api, user=HARNESS_PREFIX
        ) as app:
            assert app.workflows.runs.create({"q": "v"}).outputs == {"o": "OLD v"}
    finally:
        management.apps.delete(deployed.app_id)


@billed
def test_a_draft_run_reports_what_it_cost(management):
    """The budget checks read usage off the run, so a draft run has to carry
    it the way a Service-API run does. One short gpt-4o-mini call."""
    model = next(
        (n for n in management.models.names("llm") if n.endswith("gpt-4o-mini")), None
    )
    if model is None:
        pytest.skip("this Dify has no gpt-4o-mini configured")
    wf = Workflow(_named("rl-llm"))
    wf.depends_on(management.tools.identifier(model.split(":")[0].rsplit("/", 1)[0]))
    start = wf.start([text_input("q")])
    wf.end({"o": wf.llm(start["q"], model=model, id="llm").output})
    prompt = {"q": "Reply with the single word OK."}

    result = wf.run_live(prompt, console=management, max_tokens=2000)

    assert result.node("llm").usage.total_tokens > 0
    assert result.usage.costs, "a draft run reported no price"
    with pytest.raises(BudgetExceeded, match="over the 1 limit"):
        wf.run_live(prompt, console=management, max_tokens=1)


def test_an_unanswered_import_s_app_is_found_by_its_exact_name(management):
    """What cleanup falls back on when an import's answer is lost. Dify's
    listing matches names by substring, so an app whose name merely starts
    with ours — someone else's — must be left alone."""
    from dify_client.resources.management import _sweep_named

    ours = _named("rl-swept")
    imported = management.apps.import_definition(_greeter(ours, "Hi"))
    decoy = management.apps.import_definition(_greeter(f"{ours}-copy", "Hi"))
    try:
        assert _sweep_named(management.apps, ours).endswith("and was deleted")
        names = {app.name for app in management.apps.list(name=ours).all()}
        assert ours not in names
        assert f"{ours}-copy" in names
        assert _sweep_named(management.apps, ours).startswith("No app called")
    finally:
        management.apps.delete(decoy.app_id)
        if ours in {a.name for a in management.apps.list(name=ours).all()}:
            management.apps.delete(imported.app_id)
