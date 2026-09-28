"""A worked example of a billed test.

The free test and the billed one sit side by side and assert the same things.
The billed one creates a throwaway app in Dify from this workflow, runs it
there, and deletes it — so there is no app to set up by hand and no app key to
paste into the environment. It costs money, so it skips itself unless
``DIFY_LIVE_TESTS`` is set, and a CI job can drop it with ``-m "not billed"``.

To run it:

    export DIFY_LIVE_TESTS=1
    export DIFY_CONSOLE_TOKEN=ey…       # creates, publishes and deletes the app
    pytest tests/test_workflow_billed_example.py -m billed
"""

from dify_client import DifyManagement
from dify_client.workflow import StubLLM, Workflow, paragraph, requires_live, select

# The identifier is on the plugin's page in the Dify Marketplace. Dify installs
# the workflow's declared plugins when it imports the DSL.
OPENAI = "langgenius/openai:0.3.8@592c8252795b5f75807de2d609a03196ed02596b409f7642b4a07548c7ff57ef"


def summarizer() -> Workflow:
    wf = Workflow("summarizer")
    wf.depends_on(OPENAI)
    start = wf.start([paragraph("draft"), select("tone", ["short", "long"])])
    prompt = wf.template(
        "Summarise the following, {{ tone }}. Reply with the summary only.\n\n{{ draft }}",
        variables={"draft": start["draft"], "tone": start["tone"]},
        id="prompt",
    )
    llm = wf.llm(prompt.output, model="langgenius/openai/openai:gpt-4o-mini", id="llm")
    answer = wf.answer(llm.output)
    wf.connect(start, prompt, llm, answer)
    return wf


INPUTS = {
    "draft": "The release shipped on Friday after a two-week delay caused by a failing migration.",
    "tone": "short",
}


def check_the_prompt(result):
    """What both tests assert. The result has the same shape either way."""
    assert result.succeeded
    assert result.node("prompt")["output"].startswith("Summarise the following, short.")


def test_the_graph_works_offline():
    """The free test: everything except whether the model obeys the prompt."""
    result = summarizer().run(INPUTS, llm=StubLLM("A summary."))

    check_the_prompt(result)
    assert result["answer"] == "A summary."
    assert not result.usage, "a stubbed run must not cost anything"


@requires_live
def test_the_real_model_obeys_the_prompt():
    """The billed test: the part a stub cannot answer.

    The app is created from this workflow and deleted afterwards, so the test
    is about the code in this file rather than about whatever an app in Dify
    has drifted into.
    """
    # This workflow ends in an answer node, so Dify serves it as a chatflow:
    # `/chat-messages`, with a query as well as the start inputs. Calling
    # `workflows.runs` on it answers "check if your app mode matches".
    with DifyManagement().apps.temporary(summarizer()) as managed:
        assert managed.mode == "advanced-chat"
        result = managed.client().chat.messages.create(INPUTS["draft"], inputs=INPUTS)

    answer = result.answer
    assert result.succeeded
    assert answer.strip(), "the model returned nothing"
    # Not "shorter than the draft": a real model summarising an already-short
    # paragraph can legitimately come out longer, and asserting otherwise made
    # the only billed test fail on the model's wording rather than on this
    # code. What the prompt actually asks for is one sentence and no preamble.
    assert answer.count(".") <= 2, f"asked for one sentence, got: {answer}"
    assert not answer.lower().startswith(("here", "sure", "summary:"))

    # What it cost is part of the test, not a surprise on the invoice.
    assert result.usage.total_tokens > 0
    print(f"\nthis test spent: {result.usage}")
