"""A workflow that starts itself when a webhook is posted to.

A trigger node takes the place of the start node: nothing calls this workflow,
Dify runs it when the request arrives. That has one consequence worth knowing
before you build around it — trigger nodes live in Dify's own code rather than
in graphon, so this workflow **cannot be run locally**. The shape below is the
way around that: build the body once, behind either a start node or a trigger,
and test it through the start-node version.

**The live half costs nothing to run** — there is no model in this workflow —
but it does create an app in your workspace:

    export DIFY_LIVE_TESTS=1
    export DIFY_CONSOLE_TOKEN=ey…            # from the Dify console session
    export DIFY_HOST=http://localhost        # or your own host
    python examples/08_webhook_trigger.py

Without those it builds the workflow, exercises the testable half, and exits.
"""

import sys

from dify_client import DifyManagement
from dify_client.workflow import (
    Workflow,
    body_field,
    header,
    paragraph,
    text_input,
    why_not_live,
)


def build() -> Workflow:
    """The deployable workflow: a webhook in, a formatted line out."""
    wf = Workflow(
        "sdk-example-order-hook", description="Log an order, posted by webhook."
    )
    hook = wf.webhook(
        method="post",
        # A required header, so an unsigned request is rejected by Dify rather
        # than by the graph. Headers can only ever be strings.
        headers=[header("x-signature", required=True)],
        body=[
            body_field("order_id", required=True),
            body_field("total", "number"),
        ],
    )
    wf.connect(hook, _body(wf, hook["order_id"], hook["total"]))
    return wf


def build_for_testing() -> Workflow:
    """The same body behind a start node, so it runs offline."""
    wf = Workflow("sdk-example-order-hook (testable)")
    start = wf.start([text_input("order_id"), paragraph("total")])
    wf.connect(start, _body(wf, start["order_id"], start["total"]))
    return wf


def _body(wf: Workflow, order_id, total):
    """Everything downstream of the entry point, whichever entry point it is."""
    line = wf.template(
        "order {{ order_id }} for {{ total }}",
        variables={"order_id": order_id, "total": total},
        title="Format",
        id="format",
    )
    end = wf.end({"line": line.output})
    wf.connect(line, end)
    return line


def main() -> int:
    # The offline half: the body is a real workflow and runs right here.
    result = build_for_testing().run(
        {"order_id": "A-42", "total": "1980"}, raise_on_error=True
    )
    assert result["line"] == "order A-42 for 1980"
    print("body formats correctly (free, offline)")

    wf = build()
    print(f"deployable workflow built: {len(wf.nodes)} nodes, mode {wf.mode}")

    blocked = why_not_live()
    if blocked:
        print(f"\nstopping before touching Dify: {blocked}")
        return 0

    console = DifyManagement()
    drafted = console.apps.import_definition(wf)
    print(f"\ncreated {drafted.app_id} — stage: {drafted.stage}")

    # Import writes a draft, and a trigger in a draft is only a drawing. The
    # trigger — and the webhook's URL — exist only after publishing.
    print("triggers before publish:", console.apps.triggers.list(drafted.app_id))
    console.apps.publish(drafted.app_id)

    (trigger,) = console.apps.triggers.list(drafted.app_id)
    hook = console.apps.triggers.webhook(drafted.app_id, "trigger_webhook")
    print(f"triggers after publish : {trigger.type}, enabled={trigger.enabled}")
    print(f"\npost orders to: {hook.url}")
    print(
        "\n  curl -X POST %s \\\n"
        "    -H 'x-signature: …' -H 'Content-Type: application/json' \\\n"
        '    -d \'{"order_id": "A-42", "total": 1980}\'' % hook.url
    )

    # Pausing a trigger does not unpublish the app.
    console.apps.triggers.set_enabled(drafted.app_id, trigger.id, enabled=False)
    print(f"\npaused: enabled={console.apps.triggers.list(drafted.app_id)[0].enabled}")
    console.apps.triggers.set_enabled(drafted.app_id, trigger.id)

    print(f"\nleaving {drafted.app_id} in place — delete it with console.apps.delete()")
    return 0


if __name__ == "__main__":
    sys.exit(main())
