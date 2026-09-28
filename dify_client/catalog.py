"""What a workspace can call: model providers, and the models under them.

The same listing answers on the Service API (with a dataset key) and on the
console, so the shaping lives here rather than in either client. Two things
about it catch people out, and both are handled once, here: a label is a
localised object rather than a string, and a model does not carry the provider
it belongs to — which is the other half of what configuring a knowledge base or
an LLM node needs.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Model", "ModelProvider", "label_of", "providers_from"]


def label_of(value: Any, language: str = "en_US") -> str:
    """One readable label out of Dify's ``{"en_US": …, "zh_Hans": …}``.

    Falls back to whatever language is there rather than to an empty string: a
    provider labelled only in Chinese still has a name.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if value.get(language):
            return str(value[language])
        for other in value.values():
            if other:
                return str(other)
    return ""


@dataclass(frozen=True)
class Model:
    """One model this workspace can call."""

    #: What to pass as ``model``. Dify spells this ``model`` in the payload;
    #: it is the identifier, so it is ``name`` here and ``label`` is the
    #: human-readable one.
    name: str
    #: The provider that serves it. Not in the model's own payload — carried
    #: down from the provider it was listed under, because every call that
    #: takes a model name takes a provider beside it.
    provider: str = ""
    label: str = ""
    type: str = ""
    status: str = ""
    deprecated: bool = False
    #: Context size, max chunks and the like. Provider-specific, so a mapping.
    properties: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def usable(self) -> bool:
        """Whether Dify says this one can be called right now."""
        return self.status == "active" and not self.deprecated

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class ModelProvider:
    """A configured provider, and the models it offers.

    Iterating a provider gives its models, so a caller after "every embedding
    model" does not have to know the listing is nested.
    """

    provider: str
    label: str = ""
    status: str = ""
    models: list[Model] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def __iter__(self) -> Iterator[Model]:
        return iter(self.models)

    def __len__(self) -> int:
        return len(self.models)

    def __str__(self) -> str:
        return self.provider


def _model(payload: Mapping[str, Any], provider: str) -> Model:
    return Model(
        name=str(payload.get("model") or ""),
        provider=provider,
        label=label_of(payload.get("label")),
        type=str(payload.get("model_type") or ""),
        status=str(payload.get("status") or ""),
        deprecated=bool(payload.get("deprecated", False)),
        properties=dict(payload.get("model_properties") or {}),
        payload=dict(payload),
    )


def providers_from(items: Any) -> list[ModelProvider]:
    """Shape a model listing, from either API."""
    shaped = []
    for item in items if isinstance(items, list) else []:
        provider = str(item.get("provider") or "")
        models = item.get("models")
        shaped.append(
            ModelProvider(
                provider=provider,
                label=label_of(item.get("label")),
                status=str(item.get("status") or ""),
                models=[
                    _model(model, provider)
                    for model in (models if isinstance(models, list) else [])
                ],
                payload=dict(item),
            )
        )
    return shaped
