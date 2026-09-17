"""Branching, and asserting which way a run went.

Typed helpers exist for the common nodes; every other node type graphon
supports goes through ``wf.add()`` with its entity, which is what if-else and
the variable aggregator use here.

Two things worth copying:

- Branches are joined by a **variable aggregator**. Without one, an answer that
  reads from both branches renders the unexecuted branch's reference as literal
  text, because that variable was never produced.
- ``result.nodes`` holds only the nodes that ran, so *which path was taken* is
  something a test can assert directly.

    python examples/03_branching.py
"""

from graphon.nodes.if_else.entities import IfElseNodeData
from graphon.nodes.variable_aggregator.entities import VariableAggregatorNodeData
from graphon.utils.condition.entities import Condition

from dify_client.workflow import Workflow, paragraph


def build() -> Workflow:
    wf = Workflow("triage", description="Route a message by urgency.")
    start = wf.start([paragraph("message", label="Message")])

    branch = wf.add(
        IfElseNodeData(
            title="Urgent?",
            cases=[
                IfElseNodeData.Case(
                    case_id="urgent",
                    logical_operator="or",
                    conditions=[
                        Condition(
                            variable_selector=start["message"].selector,
                            comparison_operator="contains",
                            value=word,
                        )
                        for word in ("urgent", "outage", "down")
                    ],
                )
            ],
        ),
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
    merged = wf.add(
        VariableAggregatorNodeData(
            title="Merge",
            output_type="string",
            variables=[escalate.output.selector, queue.output.selector],
        ),
        id="merged",
    )

    answer = wf.answer(merged["output"])

    wf.connect(start, branch)
    # The handle is the case id for a matched case, and "false" for the else.
    wf.connect(branch, escalate, handle="urgent")
    wf.connect(branch, queue, handle="false")
    wf.connect(escalate, merged)
    wf.connect(queue, merged)
    wf.connect(merged, answer)
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
