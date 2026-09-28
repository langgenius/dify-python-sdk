"""Calling an HTTP API from a workflow, and testing the handling around it.

The HTTP node runs locally — graphon has its own client — so this reaches the
network but needs no Dify server. That also means a test using it is only as
reliable as the endpoint, which is why the assertions below are about the
*handling* rather than about the remote content.

    python examples/04_http_request.py
"""

from graphon.nodes.http_request.entities import (
    HttpRequestNodeAuthorization,
    HttpRequestNodeData,
)
from graphon.nodes.if_else.entities import IfElseNodeData
from graphon.nodes.variable_aggregator.entities import VariableAggregatorNodeData
from graphon.utils.condition.entities import Condition

from dify_client.workflow import Workflow, text_input


def build() -> Workflow:
    wf = Workflow("status-check", description="Report whether a URL is healthy.")
    start = wf.start([text_input("url", label="URL to check")])

    fetch = wf.add(
        HttpRequestNodeData(
            title="Fetch",
            method="get",
            url=str(start["url"]),
            authorization=HttpRequestNodeAuthorization(type="no-auth"),
            headers="",
            params="",
        ),
        id="fetch",
    )

    branch = wf.add(
        IfElseNodeData(
            title="Healthy?",
            cases=[
                IfElseNodeData.Case(
                    case_id="healthy",
                    logical_operator="and",
                    conditions=[
                        Condition(
                            variable_selector=fetch["status_code"].selector,
                            comparison_operator="=",
                            value="200",
                        )
                    ],
                )
            ],
        ),
        id="branch",
    )

    ok = wf.template(
        "{{ url }} is up ({{ code }}).",
        variables={"url": start["url"], "code": fetch["status_code"]},
        title="Healthy",
        id="ok",
    )
    bad = wf.template(
        "{{ url }} answered {{ code }} — check it.",
        variables={"url": start["url"], "code": fetch["status_code"]},
        title="Unhealthy",
        id="bad",
    )

    merged = wf.add(
        VariableAggregatorNodeData(
            title="Merge",
            output_type="string",
            variables=[ok.output.selector, bad.output.selector],
        ),
        id="merged",
    )
    answer = wf.answer(merged["output"])

    wf.connect(start, fetch, branch)
    wf.connect(branch, ok, handle="healthy")
    wf.connect(branch, bad, handle="false")
    wf.connect(ok, merged)
    wf.connect(bad, merged)
    wf.connect(merged, answer)
    return wf


def main() -> None:
    wf = build()

    for url in ("https://example.com", "https://example.com/nothing-here"):
        result = wf.run({"url": url})
        if not result.succeeded:
            print(f"{url} -> could not be reached: {result.error}")
            continue
        code = result.node("fetch")["status_code"]
        print(f"{url:42} -> HTTP {code}: {result['answer']}")

    print("\nnote: no Dify server was involved, but the network was")


if __name__ == "__main__":
    main()
