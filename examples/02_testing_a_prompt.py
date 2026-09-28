"""Testing the part of a workflow that surrounds a model.

A stub answers the LLM node, so this runs offline and for free while still
asserting on the prompt the workflow actually assembled — which is where most
prompt bugs live.

    python examples/02_testing_a_prompt.py
"""

from dify_client.workflow import StubLLM, Workflow, paragraph, select

OPENAI = "langgenius/openai:0.3.8@592c8252795b5f75807de2d609a03196ed02596b409f7642b4a07548c7ff57ef"


def build() -> Workflow:
    wf = Workflow("tone-rewriter", description="Rewrite text in a chosen tone.")
    wf.depends_on(OPENAI)

    start = wf.start(
        [
            paragraph("draft", label="Draft text"),
            select("tone", ["formal", "casual", "enthusiastic"], label="Tone"),
        ]
    )

    # Assembling the prompt in its own node is what makes it assertable
    # without involving a model at all.
    instruction = wf.template(
        "Rewrite the following in a {{ tone }} tone. Reply with the rewrite only."
        "\n\n{{ draft }}",
        variables={"draft": start["draft"], "tone": start["tone"]},
        title="Build prompt",
        id="instruction",
    )

    rewrite = wf.llm(
        instruction.output,
        model="langgenius/openai/openai:gpt-4o-mini",
        title="Rewrite",
        id="rewrite",
        completion_params={"temperature": 0.3},
    )

    answer = wf.answer(rewrite.output)
    wf.connect(start, instruction, rewrite, answer)
    return wf


INPUTS = {"draft": "we ship it tomorrow", "tone": "formal"}


def main() -> None:
    wf = build()

    # 1. A fixed reply: assert on what the workflow does with it.
    stub = StubLLM("We will be shipping tomorrow.")
    result = wf.run(INPUTS, llm=stub, raise_on_error=True)

    print("=== the prompt the workflow assembled ===")
    print(stub.calls[0][0].content)
    print(f"\nanswer: {result['answer']}")
    print(f"cost  : {result.usage}")

    assert "formal tone" in str(stub.calls[0][0].content)
    assert result.node("instruction").succeeded
    assert not result.usage, "a stubbed run must not cost anything"

    # 2. A reply that depends on the prompt: assert the tone reaches the model.
    def reply(messages):
        prompt = str(messages[0].content)
        return "HEY!" if "casual tone" in prompt else "Good day."

    for tone, expected in (("casual", "HEY!"), ("formal", "Good day.")):
        answer = wf.run({**INPUTS, "tone": tone}, llm=StubLLM(reply))["answer"]
        print(f"{tone:12} -> {answer}")
        assert answer == expected

    print("\nall assertions passed, nothing was spent")


if __name__ == "__main__":
    main()
