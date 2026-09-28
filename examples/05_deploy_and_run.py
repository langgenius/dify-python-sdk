"""Deploying a code-defined workflow to Dify and running it there.

**This one costs money.** It creates an app in your Dify workspace from the
workflow below, publishes it, mints its Service-API key, runs it against the
real model, and deletes the app again.

    export DIFY_LIVE_TESTS=1
    export DIFY_CONSOLE_TOKEN=ey…          # from the Dify console session
    export DIFY_HOST=https://cloud.dify.ai   # or your own host
    python examples/05_deploy_and_run.py

Without those it explains what is missing and exits without spending anything.
"""

import sys

from dify_client import DifyManagement
from dify_client.workflow import StubLLM, Workflow, paragraph, why_not_live

OPENAI = "langgenius/openai:0.3.8@592c8252795b5f75807de2d609a03196ed02596b409f7642b4a07548c7ff57ef"


def build() -> Workflow:
    wf = Workflow("sdk-example-summarizer", description="Summarise a paragraph.")
    # Dify installs a workflow's declared plugins when it imports the DSL.
    wf.depends_on(OPENAI)

    start = wf.start([paragraph("draft", label="Text to summarise")])
    prompt = wf.template(
        "Summarise the following in one sentence. Reply with the summary only."
        "\n\n{{ draft }}",
        variables={"draft": start["draft"]},
        title="Build prompt",
        id="prompt",
    )
    llm = wf.llm(
        prompt.output,
        model="langgenius/openai/openai:gpt-4o-mini",
        title="Summarise",
        id="llm",
    )
    # An `end` node makes this a `workflow` app, which the Service API runs on
    # inputs alone. An `answer` node would make it a chatflow, which is served
    # at /chat-messages and needs a query — see the README.
    end = wf.end({"summary": llm.output})
    wf.connect(start, prompt, llm, end)
    return wf


DRAFT = (
    "The release shipped on Friday after a two-week delay caused by a failing "
    "database migration that only reproduced under production load."
)


def main() -> int:
    wf = build()

    # The free half runs first: if the prompt is wrong, find out for nothing.
    stubbed = wf.run({"draft": DRAFT}, llm=StubLLM("A summary."), raise_on_error=True)
    assert stubbed.node("prompt")["output"].startswith("Summarise the following")
    print("prompt assembled correctly (free, offline)")

    blocked = why_not_live()
    if blocked:
        print(f"\nstopping before the billed half: {blocked}")
        return 0

    # Everything the app needs is created from the workflow and thrown away
    # with it, so there is no app id or app key to configure by hand.
    with DifyManagement().apps.temporary(wf) as managed:
        print(f"\ncreated {managed.id} ({managed.deployment.stage}), running it…")
        result = managed.client().workflows.runs.create({"draft": DRAFT})

    print(f"\nsummary: {result['summary']}")
    print(f"spent  : {result.usage}")
    print(f"nodes  : {', '.join(sorted(result.nodes))}")
    print("\napp deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
