"""Conditions: what an if-else branches on, and what a loop breaks on.

Dify writes a condition as a variable selector, an operator and a value. The
operators are its own spelling — ``≠`` rather than ``!=``, ``is`` rather than
``==`` — and a typo in one is accepted by the DSL and only noticed when the
branch silently never fires. :func:`when` checks the operator against the set
graphon declares, so a wrong one is a ``ValueError`` here instead of a branch
that quietly does nothing there.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, get_args

from graphon.utils.condition.entities import (
    Condition,
    SubCondition,
    SubVariableCondition,
    SupportedComparisonOperator,
)

from .refs import Ref, reference

__all__ = [
    "Condition",
    "OPERATORS",
    "SubCondition",
    "SubVariableCondition",
    "of_file",
    "when",
]

#: Every comparison Dify accepts, as it spells them. Read off graphon's own
#: type alias rather than restated, so a Dify release that adds one is picked
#: up by upgrading graphon.
OPERATORS: tuple[str, ...] = tuple(get_args(SupportedComparisonOperator.__value__))

#: Operators that compare nothing — a value passed with one is a mistake.
_VALUELESS = frozenset(
    {"empty", "not empty", "null", "not null", "exists", "not exists"}
)


def _literal(value: Any) -> Any:
    """Write a comparison value the way Dify's editor stores one.

    Numbers are stored as text — the node reads the *variable's* type, not the
    literal's — and graphon's model accepts only strings, lists of them, or a
    boolean. Passing ``5`` raised a pydantic error about a field the caller
    never mentioned, so it is spelled here instead.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return value


def when(
    variable: Ref,
    operator: str,
    value: Any = None,
    *,
    of: Sequence[SubCondition] = (),
    logical: str = "and",
) -> Condition:
    """One comparison, as ``wf.if_else`` and ``wf.loop`` take them::

        when(start["count"], ">", 5)          # or "5"; both are stored as text
        when(start["name"], "contains", "dify")
        when(start["notes"], "empty")

    A number is stored as text, which is what Dify's own editor does — the
    node reads the variable's type, not the literal's — so ``5`` and ``"5"``
    are the same condition.

    ``of=`` narrows a file or array variable by its own attributes::

        when(start["doc"], "exists", of=[of_file("extension", "is", ".pdf")])
    """
    if operator not in OPERATORS:
        msg = (
            f"{operator!r} is not a comparison Dify knows. "
            f"Use one of: {', '.join(OPERATORS)}."
        )
        raise ValueError(msg)
    if operator in _VALUELESS and value is not None:
        msg = f"{operator!r} compares nothing, so it takes no value."
        raise ValueError(msg)
    return Condition(
        variable_selector=reference(variable, "when()").selector,
        comparison_operator=operator,  # type: ignore[arg-type]
        value=_literal(value),
        sub_variable_condition=(
            SubVariableCondition(
                logical_operator=logical,  # type: ignore[arg-type]
                conditions=list(of),
            )
            if of
            else None
        ),
    )


def of_file(key: str, operator: str, value: Any = None) -> SubCondition:
    """One comparison against an attribute of a file, such as ``size`` or ``type``."""
    if operator not in OPERATORS:
        msg = (
            f"{operator!r} is not a comparison Dify knows. "
            f"Use one of: {', '.join(OPERATORS)}."
        )
        raise ValueError(msg)
    return SubCondition(
        key=key,
        comparison_operator=operator,  # type: ignore[arg-type]
        value=_literal(value),
    )
