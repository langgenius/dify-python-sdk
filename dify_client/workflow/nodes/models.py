"""The nodes that call a model, and the shape each gives its answer.

Three of them — an LLM, a question classifier, a parameter extractor — and
they differ in what they do with the reply rather than in how they reach the
model. graphon owns all three schemas; what is here is the model reference
Dify writes, and the rules each node adds around it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from graphon.nodes.llm.entities import (
    ContextConfig,
    LLMNodeChatModelMessage,
    LLMNodeData,
    ModelConfig,
)
from graphon.nodes.parameter_extractor.entities import (
    ParameterConfig,
    ParameterExtractorNodeData,
)
from graphon.nodes.question_classifier.entities import (
    ClassConfig,
    QuestionClassifierNodeData,
)

from ..refs import Text, render
from ._errors import NodeError

__all__ = [
    "REASONING_MODES",
    "classifier_data",
    "extractor_data",
    "llm_data",
    "parameter",
]

#: How a parameter extractor asks: in the prompt, or through a tool call.
REASONING_MODES = ("prompt", "function_call")


def parameter(
    name: str,
    type: str = "string",
    *,
    description: str = "",
    required: bool = False,
    options: Sequence[str] | None = None,
) -> ParameterConfig:
    """One field for a parameter extractor to pull out of text.

    The description is the instruction the model actually follows, so it earns
    more care than the name does.
    """
    return ParameterConfig(
        name=name,
        type=cast(Any, type),
        description=description,
        required=required,
        options=list(options) if options else None,
    )


def llm_data(
    *,
    messages: Sequence[tuple[str, Text]],
    model: tuple[str, str],
    mode: str,
    completion_params: Mapping[str, Any] | None,
    title: str,
) -> LLMNodeData:
    """An LLM node, with its prompt already in role-and-text pairs."""
    provider, name = model
    return LLMNodeData(
        title=title,
        model=ModelConfig(
            provider=provider,
            name=name,
            mode=mode,
            completion_params=dict(completion_params or {}),
        ),
        prompt_template=[
            LLMNodeChatModelMessage(role=role, text=render(text))
            for role, text in messages
        ],
        context=ContextConfig(enabled=False),
    )


def classifier_data(
    *,
    query: Sequence[str],
    classes: Sequence[tuple[str, str]],
    model: tuple[str, str],
    instruction: str | None,
    title: str,
) -> QuestionClassifierNodeData:
    """A question classifier. Each class id is an edge handle, so ids are kept."""
    if len(classes) < 2:
        msg = "A classifier needs at least two classes to choose between."
        raise NodeError(msg)
    provider, name = model
    return QuestionClassifierNodeData(
        title=title,
        query_variable_selector=list(query),
        model=ModelConfig(provider=provider, name=name, mode="chat"),
        classes=[
            ClassConfig(id=class_id, name=description, label=description)
            for class_id, description in classes
        ],
        instruction=instruction,
    )


def extractor_data(
    *,
    query: Sequence[str],
    parameters: Sequence[ParameterConfig],
    model: tuple[str, str],
    instruction: str | None,
    reasoning: str,
    title: str,
) -> ParameterExtractorNodeData:
    """A parameter extractor, which answers with the fields it was given."""
    if not parameters:
        msg = "A parameter extractor with no parameters extracts nothing."
        raise NodeError(msg)
    if reasoning not in REASONING_MODES:
        accepted = ", ".join(repr(m) for m in REASONING_MODES)
        msg = f"reasoning={reasoning!r} is not one Dify knows. Use {accepted}."
        raise NodeError(msg)
    provider, name = model
    return ParameterExtractorNodeData(
        title=title,
        query=list(query),
        model=ModelConfig(provider=provider, name=name, mode="chat"),
        parameters=list(parameters),
        instruction=instruction,
        reasoning_mode=cast(Any, reasoning),
    )
