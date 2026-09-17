"""Helpers for declaring the input variables of a Start node."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from graphon.variables.input_entities import VariableEntity, VariableEntityType


def _build(
    variable: str,
    type_: VariableEntityType,
    label: str | None,
    required: bool,
    **extra: Any,
) -> VariableEntity:
    return VariableEntity(
        variable=variable,
        label=label if label is not None else variable,
        type=type_,
        required=required,
        **{k: v for k, v in extra.items() if v is not None},
    )


def text_input(
    variable: str,
    *,
    label: str | None = None,
    required: bool = True,
    default: str | None = None,
    max_length: int | None = None,
) -> VariableEntity:
    """A single-line text input."""
    return _build(
        variable,
        VariableEntityType.TEXT_INPUT,
        label,
        required,
        default=default,
        max_length=max_length,
    )


def paragraph(
    variable: str,
    *,
    label: str | None = None,
    required: bool = True,
    default: str | None = None,
) -> VariableEntity:
    """A multi-line text input."""
    return _build(
        variable,
        VariableEntityType.PARAGRAPH,
        label,
        required,
        default=default,
    )


def number(
    variable: str,
    *,
    label: str | None = None,
    required: bool = True,
    default: float | None = None,
) -> VariableEntity:
    """A numeric input."""
    return _build(
        variable,
        VariableEntityType.NUMBER,
        label,
        required,
        default=default,
    )


def select(
    variable: str,
    options: Sequence[str],
    *,
    label: str | None = None,
    required: bool = True,
    default: str | None = None,
) -> VariableEntity:
    """A single-choice input."""
    return _build(
        variable,
        VariableEntityType.SELECT,
        label,
        required,
        options=list(options),
        default=default,
    )


def checkbox(
    variable: str,
    *,
    label: str | None = None,
    required: bool = False,
    default: bool | None = None,
) -> VariableEntity:
    """A boolean input."""
    return _build(
        variable,
        VariableEntityType.CHECKBOX,
        label,
        required,
        default=default,
    )


def file(
    variable: str,
    *,
    label: str | None = None,
    required: bool = True,
) -> VariableEntity:
    """A single file input."""
    return _build(variable, VariableEntityType.FILE, label, required)


def file_list(
    variable: str,
    *,
    label: str | None = None,
    required: bool = True,
) -> VariableEntity:
    """A multiple file input."""
    return _build(variable, VariableEntityType.FILE_LIST, label, required)
