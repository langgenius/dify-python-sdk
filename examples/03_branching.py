"""Branching, and asserting which way a run went.

Three things worth copying:

- **Only the control flow is wired by hand.** A node that reads another node's
  output has already said it runs after it, so those edges are derived;
  ``wf.connect`` is left for the part no reference implies — which arm of the
  branch to take. ``wf.edges`` shows the whole graph before it is sent.
- **Branches are joined with** ``wf.merge``, which is Dify's variable
  aggregator. Without one, an answer that reads from both arms renders the arm
  that did not run as literal ``{{#…#}}`` text, because that variable was
  never produced.
- ``result.nodes`` holds only the nodes that ran, so *which path was taken* is
  something a test can assert directly.

    python examples/03_branching.py
"""

from dify_client.workflow import Workflow, paragraph, when


def build() -> Workflow:
    wf = Workflow("triage", description="Route a message by urgency.")
    start = wf.start([paragraph("message", label="Message")])

    branch = wf.if_else(
        {
            "urgent": [
                when(start["message"], "contains", word)
                for word in ("urgent", "outage", "down")
            ]
        },
        logical="or",
        title="Urgent?",
        id="branch",
    )

    escalate = wf.template(
        "PAGE ON-CALL — {{ m }}",
        variables={"m": start["message"]},
        title="Escalate",
        id="escalate",
    )
    queue = wf.template(
        "Queued for review — {{ m }}",
        variables={"m": start["message"]},
        title="Queue",
        id="queue",
    )

    # Whichever branch ran, its output arrives here under one name.
    merged = wf.merge(escalate, queue, title="Merge", id="merged")
    wf.answer(merged)

    # The only edges nobody could infer: which arm the branch takes. The arms
    # name themselves, so the case id is never typed as a string.
    wf.connect(branch.case("urgent"), escalate)
    wf.connect(branch.false, queue)
    return wf


def main() -> None:
    wf = build()

    for message in ("the payments API is down", "please review when you can"):
        result = wf.run({"message": message}, raise_on_error=True)
        took = "escalate" if "escalate" in result.nodes else "queue"
        print(f"{message!r:32} -> [{took}] {result['answer']}")

        # The path itself is assertable, not just the final text.
        if "down" in message:
            assert "escalate" in result.nodes
            assert "queue" not in result.nodes
        else:
            assert "queue" in result.nodes
            assert "escalate" not in result.nodes

    print("\nboth paths behaved as expected")


if __name__ == "__main__":
    main()
