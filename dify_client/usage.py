"""Token and cost accounting for a workflow run.

A run that calls a real model spends money. Reporting what it spent is what
makes a billed test reviewable: the number goes in an assertion, and a
regression that quietly triples the prompt shows up as a failure rather than on
next month's invoice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def _spent(costs: Mapping[str, Decimal]) -> str:
    """Amounts as they should be read: no dangling space for an unknown unit."""
    return ", ".join(
        f"{amount} {code}".strip() for code, amount in sorted(costs.items())
    )


@dataclass(frozen=True)
class Usage:
    """What a run consumed, summed across every model call it made.

    Costs are kept per currency rather than as one number, because a workflow
    may call providers that price in different ones and adding those together
    would produce a figure that means nothing.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency: float = 0.0
    #: currency code -> amount spent in it.
    #:
    #: ``None`` means *nobody said* — which is not the same as a run that cost
    #: nothing, and reporting it as zero is how a budget check passes a run it
    #: never measured. Dify's ``workflow_finished`` event carries token counts
    #: and no price at all, so the difference is the common case rather than an
    #: edge one.
    costs: Mapping[str, Decimal] | None = None

    @classmethod
    def from_llm_usage(cls, usage: Any) -> Usage:
        """Build from a graphon ``LLMUsage``."""
        if usage is None:
            return cls()
        price = Decimal(str(getattr(usage, "total_price", 0) or 0))
        currency = getattr(usage, "currency", "") or ""
        return cls(
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
            latency=float(getattr(usage, "latency", 0.0) or 0.0),
            costs={currency: price} if price and currency else {},
        )

    @classmethod
    def from_metadata(
        cls, metadata: Mapping[str, Any] | None, elapsed: Any = 0.0
    ) -> Usage:
        """Read tokens and cost out of Dify's ``execution_metadata``.

        Blocking mode reports only a workflow-wide token count; the per-node
        figures, price included, exist only on the stream.
        """
        meta = dict(metadata or {})
        price = meta.get("total_price")
        # An amount with no currency beside it is still an amount. Recording
        # it under "" keeps the figure and says plainly that the unit is
        # unknown; dropping it made a reported cost read as "nobody said".
        currency = str(meta.get("currency") or "")
        costs: dict[str, Decimal] | None = None
        if price is not None:
            try:
                amount = Decimal(str(price))
            except (ArithmeticError, ValueError):
                amount = Decimal(0)
            # A reported zero is a reported figure. Only an absent price is
            # unknown.
            costs = {currency: amount} if amount else {}
        return cls(
            prompt_tokens=int(meta.get("prompt_tokens") or 0),
            completion_tokens=int(meta.get("completion_tokens") or 0),
            total_tokens=int(meta.get("total_tokens") or 0),
            latency=float(elapsed or 0.0),
            costs=costs,
        )

    @property
    def cost_known(self) -> bool:
        """Whether anything reported a cost at all.

        False means no figure arrived, not that the run was free.
        """
        return self.costs is not None

    @property
    def currency(self) -> str | None:
        """The currency spent in, or ``None`` when nothing or several were.

        ``""`` is its own answer: an amount arrived without a currency beside
        it, so what was spent is known and the unit is not.
        """
        costs = self.costs or {}
        return next(iter(costs)) if len(costs) == 1 else None

    @property
    def total_price(self) -> Decimal | None:
        """The amount spent, when it was all in one currency.

        ``None`` when nothing reported a cost — ``Decimal(0)`` is reserved for
        a run that something said was free. Raises when several currencies were
        involved, since there is no exchange rate here to combine them.
        """
        if self.costs is None:
            return None
        if not self.costs:
            return Decimal(0)
        if len(self.costs) > 1:
            msg = (
                f"The run spent in more than one currency ({_spent(self.costs)}). "
                "Read usage.costs."
            )
            raise ValueError(msg)
        return next(iter(self.costs.values()))

    def __add__(self, other: Usage) -> Usage:
        """Sum two figures, keeping "nobody said" out of the total.

        Unknown plus a number is that number — adding a zero the server never
        reported would turn an unmeasured half into a measured one.
        """
        if self.costs is None and other.costs is None:
            costs: dict[str, Decimal] | None = None
        else:
            costs = dict(self.costs or {})
            for code, amount in (other.costs or {}).items():
                costs[code] = costs.get(code, Decimal(0)) + amount
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            latency=self.latency + other.latency,
            costs=costs,
        )

    def __bool__(self) -> bool:
        return bool(self.total_tokens or self.costs)

    def merged_with(self, authoritative: Usage) -> Usage:
        """Combine what was observed here with a server-reported total.

        Neither source is complete on its own: Dify's run total carries tokens
        and no price, while the per-node figures carry both but only for the
        nodes this client watched. So tokens come from the total when there is
        one, and the cost from whichever reported one.
        """
        # An amount beats a zero, whichever side reported it. A run total of
        # "0" is what Dify sends for a model it has no pricing for, and taking
        # it over per-node figures that were actually charged would report a
        # billed run as free — the undercount this whole type exists to stop.
        if authoritative.costs:
            costs = authoritative.costs
        elif self.cost_known:
            costs = self.costs
        else:
            costs = authoritative.costs
        return Usage(
            prompt_tokens=authoritative.prompt_tokens or self.prompt_tokens,
            completion_tokens=authoritative.completion_tokens or self.completion_tokens,
            total_tokens=authoritative.total_tokens or self.total_tokens,
            latency=authoritative.latency or self.latency,
            costs=costs,
        )

    def __str__(self) -> str:
        if not self:
            return "no model usage"
        tokens = (
            f"{self.total_tokens:,} tokens "
            f"({self.prompt_tokens:,} in / {self.completion_tokens:,} out)"
        )
        if self.costs is None:
            return f"{tokens} · cost not reported"
        if not self.costs:
            return f"{tokens} · free"
        return f"{tokens} · {_spent(self.costs)}"
