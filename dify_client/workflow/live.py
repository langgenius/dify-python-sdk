"""Running a workflow on real Dify, on purpose.

``StubLLM`` covers the graph around a model; it cannot tell you whether the
workflow works in Dify — with the real model, the real plugins, and the real
version of the server. That answer costs money, so the path to it is gated
rather than merely available: a live run needs an explicit opt-in and an app
key, and it reports what it spent.
"""

from __future__ import annotations

import os
from decimal import Decimal

from ..console import CONSOLE_TOKEN_ENV
from ..secrets import API_KEY_ENV
from .results import RunResult
from .usage import Usage

#: Set this to 1/true/yes to allow runs that call real models and cost money.
LIVE_ENABLED_ENV = "DIFY_LIVE_TESTS"

_TRUTHY = {"1", "true", "yes", "on"}


class LiveRunError(Exception):
    """Raised when a live run was asked for but must not proceed."""


class BudgetExceeded(Exception):
    """Raised when a live run cost more than the caller allowed.

    The charge has already happened by the time this is raised — the model was
    called. What it prevents is the same overspend passing silently on every
    later run.
    """

    def __init__(self, message: str, result: RunResult):
        super().__init__(message)
        self.result = result


def live_enabled() -> bool:
    """Whether live, billed runs are allowed in this environment."""
    return (os.environ.get(LIVE_ENABLED_ENV) or "").strip().lower() in _TRUTHY


def why_not_live() -> str | None:
    """Explain why a live run cannot proceed, or ``None`` if it can.

    Useful for skip messages: it names the missing piece rather than reporting
    a flat "not configured".
    """
    if not live_enabled():
        return (
            f"{LIVE_ENABLED_ENV} is not set; live runs call real models and cost money"
        )
    # Either credential is enough, because there are two ways in: a console
    # token provisions the app the workflow describes, while an app key runs an
    # app that already exists.
    if not os.environ.get(CONSOLE_TOKEN_ENV) and not os.environ.get(API_KEY_ENV):
        return (
            f"neither {CONSOLE_TOKEN_ENV} nor {API_KEY_ENV} is set; "
            "there is no Dify app to create or to run"
        )
    return None


def requires_live(func=None):
    """Skip a test unless live runs are enabled and configured.

    ::

        from dify_client.workflow.live import requires_live

        @requires_live
        def test_the_prompt_works_on_the_real_model():
            result = wf.run_live({"q": "hello"}, max_cost="0.05")
            assert "hello" in result["answer"].lower()
    """
    import pytest

    def wrap(target):
        blocked = why_not_live()
        skip = pytest.mark.skipif(blocked is not None, reason=blocked or "")
        return pytest.mark.billed(skip(target))

    return wrap(func) if func is not None else wrap


def check_budget(
    result: RunResult,
    max_cost: Decimal | str | float | None = None,
    max_tokens: int | None = None,
) -> RunResult:
    """Raise ``BudgetExceeded`` if the run went over the caller's limits.

    ``max_cost`` needs the run to have reported a price. Not every path does,
    and a price nobody reported is not zero: passing silently there would be
    false assurance, so it is an error rather than a pass. A run something
    reported as *free* does pass. ``max_tokens`` works wherever tokens are
    counted.
    """
    usage: Usage = result.usage

    if max_tokens is not None and usage.total_tokens > max_tokens:
        msg = (
            f"The run used {usage.total_tokens:,} tokens, over the "
            f"{max_tokens:,} limit. The call has already been made; raise "
            "max_tokens or shrink the run."
        )
        raise BudgetExceeded(msg, result)

    if max_cost is None:
        return result

    if not usage.cost_known:
        if not usage.total_tokens:
            # Nothing was consumed, so nothing was spent. A stubbed run lands
            # here, which is what makes "this test cost nothing" assertable.
            return result
        msg = (
            f"max_cost was set, but nothing reported a price for this run "
            f"({usage.total_tokens:,} tokens), so there is nothing to check it "
            "against. Dify's workflow_finished event carries token counts and "
            "no cost, so a run watched only from there lands here. Use "
            "max_tokens instead, rather than trusting a check that cannot run."
        )
        raise BudgetExceeded(msg, result)

    limit = Decimal(str(max_cost))
    spent_amount = sum((usage.costs or {}).values(), Decimal(0))
    if spent_amount > limit:
        msg = (
            f"The run cost {usage}, over the {limit} limit. "
            "The charge has already been made; raise max_cost or shrink the run."
        )
        raise BudgetExceeded(msg, result)
    return result
