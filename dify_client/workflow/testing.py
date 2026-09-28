"""Run workflows in tests without calling a real model.

A workflow that contains an LLM node cannot even be built without provider
credentials, which puts the interesting half of most workflows out of reach of
a test suite. ``StubLLM`` stands in for the model so the graph around it —
prompt assembly, branching, variable flow, output shaping — can be asserted on
in CI, offline and for free.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from graphon.dsl.node_factory import SlimDslNodeFactory
from graphon.model_runtime.entities.common_entities import I18nObject
from graphon.model_runtime.entities.llm_entities import (
    LLMResult,
    LLMResultChunk,
    LLMResultChunkDelta,
    LLMUsage,
)
from graphon.model_runtime.entities.message_entities import (
    AssistantPromptMessage,
    PromptMessage,
)
from graphon.model_runtime.entities.model_entities import (
    AIModelEntity,
    FetchFrom,
    ModelPropertyKey,
    ModelType,
)

from .results import RunResult

#: A reply, or a callable that decides the reply from the prompt it received.
Reply = str | Callable[[Sequence[PromptMessage]], str]


class StubLLM:
    """A stand-in model that returns canned replies and records its calls.

    ``reply`` is either fixed text or a callable that receives the prompt
    messages the workflow actually assembled, which is what lets a test assert
    on the prompt rather than only on the answer::

        stub = StubLLM(lambda msgs: "yes" if "urgent" in str(msgs) else "no")
        result = wf.run({"text": "urgent request"}, llm=stub)
        assert stub.calls[0][-1].content.endswith("urgent request")
    """

    def __init__(
        self,
        reply: Reply = "",
        *,
        provider: str = "stub/stub/stub",
        model_name: str = "stub-model",
        context_size: int = 8192,
    ):
        self._reply = reply
        self._provider = provider
        self._model_name = model_name
        self._context_size = context_size
        self._parameters: Mapping[str, Any] = {}
        #: The prompt messages of every invocation, in order.
        self.calls: list[Sequence[PromptMessage]] = []

    # -- identity ----------------------------------------------------------

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def parameters(self) -> Mapping[str, Any]:
        return self._parameters

    @parameters.setter
    def parameters(self, value: Mapping[str, Any]) -> None:
        self._parameters = value

    @property
    def stop(self) -> Sequence[str] | None:
        return None

    def get_model_schema(self) -> AIModelEntity:
        return AIModelEntity(
            model=self._model_name,
            label=I18nObject(en_US=self._model_name),
            model_type=ModelType.LLM,
            fetch_from=FetchFrom.CUSTOMIZABLE_MODEL,
            model_properties={ModelPropertyKey.CONTEXT_SIZE: self._context_size},
        )

    def get_llm_num_tokens(self, prompt_messages: Sequence[PromptMessage]) -> int:
        return sum(len(str(m.content or "")) for m in prompt_messages) // 4

    # -- invocation --------------------------------------------------------

    def invoke_llm(
        self,
        *,
        prompt_messages: Sequence[PromptMessage],
        model_parameters: Mapping[str, Any],
        tools: Sequence[Any] | None = None,
        stop: Sequence[str] | None = None,
        stream: bool = False,
    ) -> LLMResult | Generator[LLMResultChunk, None, None]:
        self.calls.append(list(prompt_messages))
        text = self._resolve(prompt_messages)
        if stream:
            return self._stream(text, prompt_messages)
        return LLMResult(
            model=self._model_name,
            prompt_messages=list(prompt_messages),
            message=AssistantPromptMessage(content=text),
            usage=LLMUsage.empty_usage(),
        )

    def invoke_llm_with_structured_output(
        self,
        *,
        prompt_messages: Sequence[PromptMessage],
        json_schema: Mapping[str, Any],
        model_parameters: Mapping[str, Any],
        stop: Sequence[str] | None = None,
        stream: bool = False,
    ) -> Any:
        msg = (
            "StubLLM does not implement structured output. "
            "Give the node a reply that already parses, or stub the node itself."
        )
        raise NotImplementedError(msg)

    def is_structured_output_parse_error(self, error: Exception) -> bool:
        return False

    # -- internals ---------------------------------------------------------

    def _resolve(self, prompt_messages: Sequence[PromptMessage]) -> str:
        if callable(self._reply):
            return self._reply(prompt_messages)
        return self._reply

    def _stream(
        self,
        text: str,
        prompt_messages: Sequence[PromptMessage],
    ) -> Generator[LLMResultChunk, None, None]:
        yield LLMResultChunk(
            model=self._model_name,
            prompt_messages=list(prompt_messages),
            delta=LLMResultChunkDelta(
                index=0,
                message=AssistantPromptMessage(content=text),
                usage=LLMUsage.empty_usage(),
                finish_reason="stop",
            ),
        )


@contextmanager
def stub_models(llm: StubLLM) -> Generator[None, None, None]:
    """Make every LLM node in a workflow use ``llm`` for the duration.

    graphon resolves provider credentials while it builds LLM nodes, so the
    substitution has to happen at build time rather than at invocation time.
    """
    # Credentials are demanded from two places: validate_node() checks them
    # before the graph is built, and _create_slim_llm_runtime() uses them to
    # construct the real client. Both have to be neutralised.
    originals = {
        name: getattr(SlimDslNodeFactory, name)
        for name in ("_resolve_slim_llm_settings", "_create_slim_llm_runtime")
    }

    def _resolve(
        self: SlimDslNodeFactory,
        *,
        node_id: str,
        data: Mapping[str, Any],
        node_type_label: str,
    ) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
        return dict(data), llm.provider, "stub", {}

    def _create(
        self: SlimDslNodeFactory,
        *,
        node_id: str,
        data: Mapping[str, Any],
        node_type_label: str,
    ) -> tuple[dict[str, Any], StubLLM]:
        return dict(data), llm

    SlimDslNodeFactory._resolve_slim_llm_settings = _resolve  # type: ignore[method-assign]
    SlimDslNodeFactory._create_slim_llm_runtime = _create  # type: ignore[method-assign]
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(SlimDslNodeFactory, name, original)


def run_with_stub(
    dsl: str,
    *,
    llm: StubLLM,
    inputs: Mapping[str, Any] | None = None,
    workflow_id: str | None = None,
) -> RunResult:
    """Run a DSL document with every LLM node answered by ``llm``."""
    from .runner import run_dsl

    with stub_models(llm):
        return run_dsl(dsl, inputs=inputs, workflow_id=workflow_id)
