"""Nodes that decide, repeat, gather and assign — the graph's own plumbing.

None of these call a model or reach outside Dify; what they do is shape how
the rest of the graph runs. graphon owns all of their schemas, so what is here
is the spelling Dify uses and the rules it applies: the operators an assigner
has, the error modes an iteration has, the fact that a branch with no
conditions is a branch that always goes the same way.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from graphon.nodes.if_else.entities import IfElseNodeData
from graphon.nodes.iteration.entities import IterationNodeData
from graphon.nodes.list_operator.entities import (
    ExtractConfig,
    FilterBy,
    FilterCondition,
    Limit,
    ListOperatorNodeData,
    OrderByConfig,
)
from graphon.nodes.loop.entities import LoopNodeData, LoopVariableData
from graphon.nodes.variable_aggregator.entities import VariableAggregatorNodeData
from graphon.nodes.variable_assigner.v2.entities import (
    VariableAssignerNodeData,
    VariableOperationItem,
)
from graphon.utils.condition.entities import Condition

from ..refs import Ref, is_reference, reference
from ._errors import NodeError

__all__ = [
    "ASSIGN_OPERATIONS",
    "ERROR_MODES",
    "aggregate_data",
    "assign_data",
    "if_else_data",
    "iteration_data",
    "list_operator_data",
    "loop_data",
    "loop_var",
]

#: What an assigner may do to a variable, as Dify spells each one.
ASSIGN_OPERATIONS = (
    "over-write",
    "clear",
    "append",
    "extend",
    "set",
    "+=",
    "-=",
    "*=",
    "/=",
    "remove-first",
    "remove-last",
)

#: The arm an if-else takes when no case matched, as graphon names it.
ELSE_HANDLE = "false"

#: What an iteration does when one pass fails.
ERROR_MODES = ("terminated", "continue-on-error", "remove-abnormal-output")


def if_else_data(
    *,
    conditions: Sequence[Condition] | Mapping[str, Sequence[Condition]],
    logical: str,
    title: str,
) -> IfElseNodeData:
    """A branch. A mapping builds the ELIF chain, one case per key."""
    if isinstance(conditions, Mapping) and ELSE_HANDLE in conditions:
        # graphon continues along "false" when no case matched, so a case of
        # that name is indistinguishable from the else arm: two arms share one
        # handle, and one of them is never taken.
        msg = (
            f"{ELSE_HANDLE!r} is the arm an if-else takes when no case "
            "matched, so it cannot also name a case. Call the case something "
            "else — its key is the handle you connect from."
        )
        raise NodeError(msg)
    if isinstance(conditions, Mapping):
        cases = [
            IfElseNodeData.Case(
                case_id=case_id,
                logical_operator=cast(Any, logical),
                conditions=list(group),
            )
            for case_id, group in conditions.items()
        ]
    else:
        cases = [
            IfElseNodeData.Case(
                case_id="true",
                logical_operator=cast(Any, logical),
                conditions=list(conditions),
            )
        ]
    if not cases or not any(case.conditions for case in cases):
        msg = (
            "An if-else node with no conditions always takes the same "
            "branch. Add one with when(node['field'], 'is', 'value')."
        )
        raise NodeError(msg)
    return IfElseNodeData(title=title, cases=cases)


def iteration_data(
    *,
    node_id: str,
    items: Sequence[str],
    parallel: bool,
    parallel_nums: int,
    on_error: str,
    flatten: bool,
    title: str,
) -> IterationNodeData:
    """An iteration. Its body begins at a start marker named after it."""
    if on_error not in ERROR_MODES:
        accepted = ", ".join(repr(mode) for mode in ERROR_MODES)
        msg = f"on_error={on_error!r} is not one Dify knows. Use {accepted}."
        raise NodeError(msg)
    return IterationNodeData(
        title=title,
        start_node_id=f"{node_id}start",
        iterator_selector=list(items),
        # Filled in by returns() before the container closes; an iteration
        # that never says what a pass contributes is refused there.
        output_selector=[],
        is_parallel=parallel,
        parallel_nums=parallel_nums,
        error_handle_mode=cast(Any, on_error),
        flatten_output=flatten,
    )


def loop_data(
    *,
    node_id: str,
    until: Sequence[Condition],
    count: int,
    logical: str,
    variables: Sequence[LoopVariableData],
    title: str,
) -> LoopNodeData:
    """A loop. ``count`` bounds it whatever ``until`` says, as Dify requires."""
    return LoopNodeData(
        title=title,
        start_node_id=f"{node_id}start",
        loop_count=count,
        break_conditions=list(until),
        logical_operator=cast(Any, logical),
        loop_variables=list(variables),
    )


def loop_var(name: str, value: Any, *, type: str = "string") -> LoopVariableData:
    """A value a loop carries from one pass to the next.

    A reference — a node's field, or a node — starts the loop from that
    variable; anything else is a constant.
    """
    is_variable = is_reference(value)
    return LoopVariableData(
        label=name,
        var_type=cast(Any, type),
        value_type="variable" if is_variable else "constant",
        value=reference(value, name).selector if is_variable else value,
    )


def aggregate_data(
    *, variables: Sequence[Ref], output_type: str, title: str
) -> VariableAggregatorNodeData:
    """Whichever branch ran, its output arrives here under one name."""
    if not variables:
        msg = "A variable aggregator needs variables to aggregate."
        raise NodeError(msg)
    return VariableAggregatorNodeData(
        title=title,
        output_type=output_type,
        variables=[reference(ref, "aggregate()").selector for ref in variables],
    )


def assign_data(
    *, assignments: Sequence[tuple[Ref, str, Any]], title: str
) -> VariableAssignerNodeData:
    """Writes to the variables that outlive a node — conversation variables."""
    if not assignments:
        msg = "A variable assigner with no assignments writes nothing."
        raise NodeError(msg)
    items = []
    for target, operation, value in assignments:
        if operation not in ASSIGN_OPERATIONS:
            accepted = ", ".join(sorted(ASSIGN_OPERATIONS))
            msg = (
                f"{operation!r} is not an assignment Dify knows. "
                f"Use one of: {accepted}."
            )
            raise NodeError(msg)
        is_variable = is_reference(value)
        items.append(
            VariableOperationItem(
                variable_selector=reference(target, "assign()").selector,
                input_type=cast(Any, "variable" if is_variable else "constant"),
                operation=cast(Any, operation),
                value=reference(value, "assign()").selector if is_variable else value,
            )
        )
    return VariableAssignerNodeData(title=title, items=items)


def list_operator_data(
    *,
    variable: Sequence[str],
    where: Sequence[FilterCondition],
    order_by: str | None,
    descending: bool,
    limit: int | None,
    extract: int | None,
    title: str,
) -> ListOperatorNodeData:
    """Filter, sort and cut an array.

    Each part carries its own enable flag, so leaving one out is the setting
    rather than the absence of one.
    """
    if extract is not None and extract < 1:
        msg = f"extract={extract!r} is not a position. Dify counts from 1."
        raise NodeError(msg)
    return ListOperatorNodeData(
        title=title,
        variable=list(variable),
        filter_by=FilterBy(enabled=bool(where), conditions=list(where)),
        order_by=OrderByConfig(
            enabled=order_by is not None,
            key=order_by or "",
            value=cast(Any, "desc" if descending else "asc"),
        ),
        limit=Limit(enabled=limit is not None, size=limit if limit is not None else -1),
        extract_by=ExtractConfig(
            enabled=extract is not None,
            serial=str(extract) if extract is not None else "1",
        ),
    )
