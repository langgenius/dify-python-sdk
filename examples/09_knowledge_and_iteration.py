"""Retrieving from a knowledge base, then working through what came back.

Two node types that need care:

- **Knowledge retrieval runs inside Dify.** The index, the embedding model and
  the rerank model are the server's, so graphon has nothing to call and a local
  run answers the node from ``StubKnowledge``. That makes the *workflow* around
  retrieval testable offline; it says nothing about what a real index returns.
- **An iteration is a container.** Nodes built inside the ``with`` block belong
  to it, and ``returns()`` names what each pass contributes to its output.

Set ``DATASET_ID`` to a real knowledge base before deploying this — the id is
the one thing here a local run does not check. ``DifyKnowledge.datasets.list()``
shows what a workspace has.

    python examples/09_knowledge_and_iteration.py
"""

from dify_client.workflow import StubCode, StubKnowledge, Workflow, text_input

#: Replace with a real one from DifyKnowledge.datasets.list() before deploying.
DATASET_ID = "00000000-0000-0000-0000-000000000000"


def build() -> Workflow:
    wf = Workflow("faq-digest", description="Answer from a knowledge base.")
    start = wf.start([text_input("question", label="Question")])

    hits = wf.knowledge(start["question"], [DATASET_ID], top_k=3, id="hits")

    # The node's `result` is an array of objects; a code node turns it into the
    # array of strings the iteration walks.
    contents = wf.code(
        "def main(hits):\n    return {'lines': [h['content'] for h in hits]}",
        variables={"hits": hits.output},
        outputs={"lines": "array[string]"},
        title="Contents",
        id="contents",
    )

    with wf.iteration(contents["lines"], title="Each hit", id="each") as each:
        quoted = wf.template(
            "- {{ line }}",
            variables={"line": each.item},
            title="Quote",
            id="quote",
        )
        each.returns(quoted.output)

    end = wf.end({"citations": each.output}, id="end")
    wf.connect(start, hits, contents, each, end)
    return wf


def main() -> None:
    wf = build()

    # Retrieval and the sandbox are both the server's; stand in for both.
    knowledge = StubKnowledge(
        [
            "Refunds are issued within 5 business days.",
            "Refunds go back to the original payment method.",
        ]
    )
    code = StubCode(
        {
            "lines": [
                "Refunds are issued within 5 business days.",
                "Refunds go back to the original payment method.",
            ]
        }
    )

    result = wf.run(
        {"question": "how long do refunds take?"}, knowledge=knowledge, code=code
    )
    result.raise_for_status()

    print(f"asked: {knowledge.calls[0].query!r}")
    print(f"bases: {knowledge.calls[0].dataset_ids}")
    for line in result["citations"]:
        print(line)

    # The iteration ran once per hit, which is the part worth asserting.
    assert len(result["citations"]) == 2
    assert sum(1 for run in result.executions if run.node_id == "quote") == 2
    print("\nthe iteration ran once per retrieved chunk")


if __name__ == "__main__":
    main()
