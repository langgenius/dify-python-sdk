"""The human-input node: a run that pauses until a person answers a form.

graphon carries the node type and nothing else — the form, its fields and its
buttons are Dify's schema — so they are restated here from
``core.workflow.nodes.human_input``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, cast

from graphon.entities.base_node_data import BaseNodeData
from graphon.enums import BuiltinNodeTypes
from pydantic import BaseModel, Field

from ..refs import Node, VarRef
from ._errors import NodeError

__all__ = [
    "FileInputConfig",
    "FileListInputConfig",
    "FormInputConfig",
    "HumanInputNodeData",
    "ParagraphInputConfig",
    "SelectInputConfig",
    "StringListSource",
    "StringSource",
    "UserActionConfig",
    "TIMEOUT_UNITS",
    "action",
    "form_data",
    "form_file",
    "form_files",
    "form_paragraph",
    "form_select",
]


class StringSource(BaseModel):
    """A string that is either written here or read from a variable."""

    type: Literal["constant", "variable"]
    selector: Sequence[str] = Field(default_factory=tuple)
    value: str = ""


class StringListSource(BaseModel):
    """A list of strings, literal or read from an ``array[string]`` variable."""

    type: Literal["constant", "variable"]
    selector: Sequence[str] = Field(default_factory=tuple)
    value: list[str] = Field(default_factory=list)


class ParagraphInputConfig(BaseModel):
    """A free-text field on a human-input form."""

    type: Literal["paragraph"] = "paragraph"
    output_variable_name: str
    default: StringSource | None = None


class SelectInputConfig(BaseModel):
    """A single-choice field on a human-input form."""

    type: Literal["select"] = "select"
    output_variable_name: str
    option_source: StringListSource


class FileInputConfig(BaseModel):
    """A single-file field on a human-input form."""

    type: Literal["file"] = "file"
    output_variable_name: str
    allowed_file_types: Sequence[str] = Field(default_factory=list)
    allowed_file_extensions: Sequence[str] = Field(default_factory=list)
    allowed_file_upload_methods: Sequence[str] = Field(default_factory=list)


class FileListInputConfig(BaseModel):
    """A multi-file field on a human-input form."""

    type: Literal["file-list"] = "file-list"
    output_variable_name: str
    allowed_file_types: Sequence[str] = Field(default_factory=list)
    allowed_file_extensions: Sequence[str] = Field(default_factory=list)
    allowed_file_upload_methods: Sequence[str] = Field(default_factory=list)
    number_limits: int = 0


FormInputConfig = (
    ParagraphInputConfig | SelectInputConfig | FileInputConfig | FileListInputConfig
)


class UserActionConfig(BaseModel):
    """One button on the form. Its ``id`` is the edge handle it continues along."""

    id: str
    title: str
    button_style: Literal["primary", "default", "accent", "ghost"] = "default"


class HumanInputNodeData(BaseNodeData):
    """A node that pauses the run until a person fills in a form.

    ``form_content`` is the markdown shown to them; ``{{#$output.field#}}``
    inside it renders a field of the same name. Each action becomes a branch,
    handled by its id, and the run also continues along ``timeout`` when
    nobody answers in time.
    """

    type: str = BuiltinNodeTypes.HUMAN_INPUT
    form_content: str = ""
    inputs: list[FormInputConfig] = Field(default_factory=list)
    user_actions: list[UserActionConfig] = Field(default_factory=list)
    timeout: int = 36
    timeout_unit: Literal["hour", "day"] = "hour"


def form_paragraph(
    name: str,
    *,
    default: str | VarRef | Node | Sequence[str] | None = None,
) -> ParagraphInputConfig:
    """A text field. ``default`` is literal text or a reference to some::

    form_paragraph("note")
    form_paragraph("note", default=draft["text"])
    """
    source: StringSource | None = None
    if isinstance(default, str):
        source = StringSource(type="constant", value=default)
    elif isinstance(default, (VarRef, Node)):
        ref = default if isinstance(default, VarRef) else default.output
        source = StringSource(type="variable", selector=ref.selector)
    elif default is not None:
        source = StringSource(type="variable", selector=list(default))
    return ParagraphInputConfig(output_variable_name=name, default=source)


def form_select(name: str, options: VarRef | Node | Sequence[str]) -> SelectInputConfig:
    """A dropdown, from a fixed list or from a variable filled at run time::

        form_select("size", ["small", "large"])
        form_select("topic", classify["topics"])   # an array[string] output

    A reference builds the options when the form is shown, which is how a
    dropdown offers what the run just found rather than what was known when
    the workflow was written.
    """
    if isinstance(options, Node):
        options = options.output
    if isinstance(options, VarRef):
        return SelectInputConfig(
            output_variable_name=name,
            option_source=StringListSource(type="variable", selector=options.selector),
        )
    if isinstance(options, str):
        # list("abc") is three options, which is never what was meant.
        msg = (
            f"options={options!r} is one string, not a list of choices. Pass "
            f"[{options!r}] for a single option, or a reference to an "
            "array[string] output to fill the dropdown at run time."
        )
        raise NodeError(msg)
    return SelectInputConfig(
        output_variable_name=name,
        option_source=StringListSource(type="constant", value=list(options)),
    )


def form_file(
    name: str,
    *,
    types: Sequence[str] = (),
    extensions: Sequence[str] = (),
    methods: Sequence[str] = ("local_file", "remote_url"),
) -> FileInputConfig:
    """A file upload field."""
    return FileInputConfig(
        output_variable_name=name,
        allowed_file_types=list(types),
        allowed_file_extensions=list(extensions),
        allowed_file_upload_methods=list(methods),
    )


def form_files(
    name: str,
    *,
    limit: int = 0,
    types: Sequence[str] = (),
    extensions: Sequence[str] = (),
    methods: Sequence[str] = ("local_file", "remote_url"),
) -> FileListInputConfig:
    """A field that takes several files. ``limit=0`` means Dify's own maximum."""
    return FileListInputConfig(
        output_variable_name=name,
        allowed_file_types=list(types),
        allowed_file_extensions=list(extensions),
        allowed_file_upload_methods=list(methods),
        number_limits=limit,
    )


def action(id: str, title: str, *, style: str = "default") -> UserActionConfig:
    """One button, and the branch it continues along."""
    return UserActionConfig(id=id, title=title, button_style=style)  # type: ignore[arg-type]


# -- building one ----------------------------------------------------------

#: How long Dify may wait, in units it understands.
TIMEOUT_UNITS = ("hour", "day")


def form_data(
    *,
    form_content: str,
    inputs: Sequence[FormInputConfig],
    actions: Sequence[UserActionConfig],
    timeout: int,
    timeout_unit: str,
    title: str,
) -> HumanInputNodeData:
    """A form that pauses the run, and the arms it continues along.

    A form with no action has no way to continue: Dify shows it, the person
    reads it, and the run waits until it times out. That is refused here
    rather than discovered on a paused run.
    """
    if timeout_unit not in TIMEOUT_UNITS:
        accepted = ", ".join(repr(unit) for unit in TIMEOUT_UNITS)
        msg = f"timeout_unit={timeout_unit!r} is not one Dify knows. Use {accepted}."
        raise NodeError(msg)
    if not actions:
        msg = (
            "A human-input node with no actions has no way to continue. "
            "Add one with action('approve', 'Approve')."
        )
        raise NodeError(msg)
    return HumanInputNodeData(
        title=title,
        form_content=form_content,
        inputs=list(inputs),
        user_actions=list(actions),
        timeout=timeout,
        timeout_unit=cast(Any, timeout_unit),
    )
