"""Gating, deployment preflight and budget checks for billed runs.

None of these call Dify: they exercise the machinery that decides whether a
billed call may be made, which is the part that must not regress.
"""

from decimal import Decimal

import pytest

from dify_client.console import CONSOLE_TOKEN_ENV
from dify_client.secrets import API_KEY_ENV
from dify_client.workflow import (
    BudgetExceeded,
    LiveRunError,
    StubLLM,
    Usage,
    Workflow,
    live_enabled,
    text_input,
    why_not_live,
)
from dify_client.workflow.live import LIVE_ENABLED_ENV, check_budget
from dify_client.workflow.results import NodeResult, RunResult

APP_KEY = "app-LIVEKEY1234567890"
PLUGIN = "langgenius/openai:0.3.8@abc123"


@pytest.fixture(autouse=True)
def _no_ambient_live_config(monkeypatch):
    monkeypatch.delenv(LIVE_ENABLED_ENV, raising=False)
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.delenv(CONSOLE_TOKEN_ENV, raising=False)


def llm_workflow(*, with_plugin: bool = False) -> Workflow:
    wf = Workflow("live-app")
    if with_plugin:
        wf.depends_on(PLUGIN)
    start = wf.start([text_input("q")])
    llm = wf.llm(start["q"], model="langgenius/openai/openai:gpt-4o-mini", id="llm")
    answer = wf.answer(llm.output)
    wf.connect(start, llm, answer)
    return wf


class FakeApps:
    """The apps resource, recording what it was asked to deploy."""

    def __init__(self, console):
        self._console = console

    def deploy(self, workflow, *, app_id=None, name=None, key=True, publish=True):
        from dify_client.lifecycle import Deployment

        self._console.deployed.append((workflow.name, app_id))
        # run_live needs a published app, so that is what a deploy reports.
        return Deployment(
            imported=True, published=True, app_id=app_id or "new", app_mode="workflow"
        )


class FakeConsole:
    """Stands in for DifyManagement; records what it was asked to deploy."""

    def __init__(self):
        self.deployed: list[tuple[str, str | None]] = []
        self.apps = FakeApps(self)


class TestTheGate:
    def test_live_runs_are_off_by_default(self):
        assert not live_enabled()

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_the_flag_accepts_the_usual_spellings(self, value, monkeypatch):
        monkeypatch.setenv(LIVE_ENABLED_ENV, value)
        assert live_enabled()

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "  ", "maybe"])
    def test_anything_else_keeps_them_off(self, value, monkeypatch):
        monkeypatch.setenv(LIVE_ENABLED_ENV, value)
        assert not live_enabled()

    def test_the_flag_alone_is_not_enough(self, monkeypatch):
        """Both gates must be open, so one stray variable cannot start spending."""
        monkeypatch.setenv(LIVE_ENABLED_ENV, "1")
        assert API_KEY_ENV in why_not_live()
        assert CONSOLE_TOKEN_ENV in why_not_live()

    def test_a_credential_alone_is_not_enough(self, monkeypatch):
        monkeypatch.setenv(API_KEY_ENV, APP_KEY)
        assert LIVE_ENABLED_ENV in why_not_live()

    def test_an_app_key_opens_the_second_gate(self, monkeypatch):
        """Enough to run an app that already exists."""
        monkeypatch.setenv(LIVE_ENABLED_ENV, "1")
        monkeypatch.setenv(API_KEY_ENV, APP_KEY)
        assert why_not_live() is None

    def test_a_console_token_opens_it_too(self, monkeypatch):
        """Enough to create the app the workflow describes, key and all."""
        monkeypatch.setenv(LIVE_ENABLED_ENV, "1")
        monkeypatch.setenv(CONSOLE_TOKEN_ENV, "ey.FAKE.TOKEN")
        assert why_not_live() is None

    def test_the_reason_explains_the_cost(self):
        assert "cost money" in why_not_live()


class TestRunLive:
    def test_it_refuses_while_the_gate_is_closed(self):
        with pytest.raises(LiveRunError, match=LIVE_ENABLED_ENV):
            llm_workflow().run_live({"q": "hi"})

    def test_the_refusal_points_at_the_free_alternative(self):
        with pytest.raises(LiveRunError, match="StubLLM"):
            llm_workflow().run_live({"q": "hi"})

    def test_the_gate_applies_even_with_a_key_in_hand(self, monkeypatch):
        """Holding a key is not permission to spend in this environment."""
        monkeypatch.setenv(API_KEY_ENV, APP_KEY)
        with pytest.raises(LiveRunError, match=LIVE_ENABLED_ENV):
            llm_workflow().run_live({"q": "hi"})


class TestDeployingBeforeRunning:
    def test_a_console_without_an_app_id_is_refused(self, monkeypatch):
        monkeypatch.setenv(LIVE_ENABLED_ENV, "1")
        with pytest.raises(LiveRunError, match="app_id"):
            llm_workflow(with_plugin=True).run_live({"q": "hi"}, console=FakeConsole())

    def test_an_app_id_without_a_console_is_refused(self, monkeypatch):
        """Otherwise it reads as "deploy here" while deploying nothing."""
        monkeypatch.setenv(LIVE_ENABLED_ENV, "1")
        with pytest.raises(LiveRunError, match="nothing to deploy with"):
            llm_workflow(with_plugin=True).run_live({"q": "hi"}, app_id="a1")

    def test_the_workflow_is_deployed_over_the_named_app(self, monkeypatch):
        monkeypatch.setenv(LIVE_ENABLED_ENV, "1")
        console = FakeConsole()
        wf = llm_workflow(with_plugin=True)
        with pytest.raises(
            Exception
        ):  # noqa: B017,PT011 - the run itself is not stubbed
            wf.run_live({"q": "hi"}, console=console, app_id="a1")
        assert console.deployed == [("live-app", "a1")]


class TestPluginPreflight:
    def test_an_undeclared_provider_is_reported(self):
        assert llm_workflow().missing_plugin_dependencies() == ["langgenius/openai"]

    def test_declaring_the_plugin_satisfies_it(self):
        assert llm_workflow(with_plugin=True).missing_plugin_dependencies() == []

    def test_a_workflow_without_model_nodes_needs_nothing(self):
        wf = Workflow("plain")
        start = wf.start([text_input("q")])
        answer = wf.answer(start["q"])
        wf.connect(start, answer)
        assert wf.missing_plugin_dependencies() == []

    def test_deploying_without_the_plugin_is_refused(self, monkeypatch):
        """Dify would import an app that cannot run; say so before deploying."""
        monkeypatch.setenv(LIVE_ENABLED_ENV, "1")
        console = FakeConsole()
        with pytest.raises(LiveRunError, match="depends_on"):
            llm_workflow().run_live({"q": "hi"}, console=console, app_id="a1")
        assert console.deployed == []


class TestBudget:
    def _result(
        self, price: str | None, currency: str = "USD", tokens: int = 100
    ) -> RunResult:
        costs = {currency: Decimal(price)} if price is not None else {}
        node = NodeResult(
            node_id="llm",
            node_type="llm",
            status="succeeded",
            usage=Usage(total_tokens=tokens, costs=costs),
        )
        return RunResult(status="succeeded", nodes={"llm": node})

    def test_no_limit_means_no_check(self):
        result = self._result("9.99")
        assert check_budget(result) is result

    def test_a_run_within_budget_passes(self):
        assert check_budget(self._result("0.004"), "0.05") is not None

    def test_a_run_over_budget_raises(self):
        with pytest.raises(BudgetExceeded, match="over the 0.05 limit"):
            check_budget(self._result("0.5"), "0.05")

    def test_the_error_carries_the_result_for_inspection(self):
        with pytest.raises(BudgetExceeded) as caught:
            check_budget(self._result("0.5"), "0.05")
        assert caught.value.result.node("llm").usage.total_tokens == 100

    def test_the_error_says_the_charge_already_happened(self):
        with pytest.raises(BudgetExceeded, match="already been made"):
            check_budget(self._result("0.5"), "0.05")

    def test_spending_in_several_currencies_is_still_bounded(self):
        result = self._result("0.03")
        result.nodes["llm"].usage = result.nodes["llm"].usage + Usage(
            costs={"JPY": Decimal("4")}
        )
        with pytest.raises(BudgetExceeded):
            check_budget(result, "0.05")


class TestTokenBudget:
    def _result(self, tokens: int) -> RunResult:
        node = NodeResult(
            node_id="llm",
            node_type="llm",
            status="succeeded",
            usage=Usage(total_tokens=tokens),
        )
        return RunResult(status="succeeded", nodes={"llm": node})

    def test_a_run_within_the_token_limit_passes(self):
        assert check_budget(self._result(500), None, 2000) is not None

    def test_a_run_over_the_token_limit_raises(self):
        with pytest.raises(BudgetExceeded, match="over the 2,000 limit"):
            check_budget(self._result(5000), None, 2000)

    def test_tokens_are_checked_even_when_no_price_was_reported(self):
        """Dify reports tokens everywhere but price only per node."""
        with pytest.raises(BudgetExceeded):
            check_budget(self._result(5000), max_tokens=2000)


class TestMaxCostWithoutAPrice:
    def test_a_priced_check_that_cannot_run_is_not_a_silent_pass(self):
        """Passing here would be false assurance, so it is an error."""
        node = NodeResult(
            node_id="llm",
            node_type="llm",
            status="succeeded",
            usage=Usage(total_tokens=5000),
        )
        result = RunResult(status="succeeded", nodes={"llm": node})
        with pytest.raises(BudgetExceeded, match="nothing reported a price"):
            check_budget(result, "0.05")

    def test_the_error_points_at_max_tokens(self):
        node = NodeResult(
            node_id="llm",
            node_type="llm",
            status="succeeded",
            usage=Usage(total_tokens=5000),
        )
        result = RunResult(status="succeeded", nodes={"llm": node})
        with pytest.raises(BudgetExceeded, match="max_tokens"):
            check_budget(result, "0.05")


class TestStubbedRunsCostNothing:
    def test_a_stubbed_run_reports_no_usage(self):
        result = llm_workflow().run({"q": "hi"}, llm=StubLLM("ok"))
        assert not result.usage
        assert result.usage.total_price == Decimal(0)

    def test_that_makes_spending_nothing_assertable(self):
        """Zero tokens means nothing was consumed, so a price check is satisfied."""
        result = llm_workflow().run({"q": "hi"}, llm=StubLLM("ok"))
        assert check_budget(result, "0") is result
        assert check_budget(result, max_tokens=0) is result


class TestFreeIsNotUnknown:
    """A price nobody reported is not a price of zero, and a budget check that
    cannot run must not read as a pass."""

    def _run(self, usage):
        return RunResult(status="succeeded", reported_usage=usage)

    def test_an_unreported_price_refuses_the_check(self):
        with pytest.raises(BudgetExceeded, match="nothing reported a price"):
            check_budget(self._run(Usage(total_tokens=100)), "0.05")

    def test_a_price_reported_as_zero_passes(self):
        """Something said it was free. That is an answer."""
        assert check_budget(self._run(Usage(total_tokens=100, costs={})), "0.05")

    def test_a_stubbed_run_still_passes(self):
        assert check_budget(self._run(Usage()), "0.05")

    def test_the_two_states_are_distinguishable(self):
        assert Usage(costs={}).cost_known
        assert not Usage().cost_known
        assert Usage(costs={}).total_price == Decimal(0)
        assert Usage().total_price is None

    def test_a_streamed_run_keeps_the_price_the_nodes_reported(self):
        """Dify's workflow_finished carries tokens and no cost, so preferring
        it wholesale threw the price away on every streamed run."""
        node = NodeResult(
            node_id="llm",
            node_type="llm",
            status="succeeded",
            usage=Usage(total_tokens=100, costs={"USD": Decimal("0.01")}),
        )
        result = RunResult(
            status="succeeded",
            nodes={"llm": node},
            executions=[node],
            reported_usage=Usage(total_tokens=100),
        )

        assert result.usage.total_price == Decimal("0.01")
        assert result.usage.total_tokens == 100

    def test_the_run_total_still_wins_for_tokens(self):
        """It covers nodes this client never watched."""
        node = NodeResult(
            node_id="llm",
            node_type="llm",
            status="succeeded",
            usage=Usage(total_tokens=30, costs={"USD": Decimal("0.01")}),
        )
        result = RunResult(
            status="succeeded",
            executions=[node],
            reported_usage=Usage(total_tokens=300),
        )

        assert result.usage.total_tokens == 300
        assert result.node_usage.total_tokens == 30

    def test_adding_unknown_to_a_price_does_not_invent_a_zero(self):
        known = Usage(total_tokens=1, costs={"USD": Decimal("0.01")})
        assert (Usage(total_tokens=1) + known).total_price == Decimal("0.01")

    def test_adding_two_unknowns_stays_unknown(self):
        assert not (Usage(total_tokens=1) + Usage(total_tokens=1)).cost_known
