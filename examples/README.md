# Examples

Each file runs on its own:

```bash
pip install "dify-client[workflow]"
python examples/01_hello_workflow.py
```

`tests/test_examples.py` builds and runs every one of them, so what is here
keeps working.

| | What it shows | Needs |
|---|---|---|
| [01_hello_workflow.py](01_hello_workflow.py) | Define a workflow, run it, export the DSL | nothing |
| [02_testing_a_prompt.py](02_testing_a_prompt.py) | Assert on the prompt a workflow assembles, with `StubLLM` | nothing |
| [03_branching.py](03_branching.py) | if-else, joining branches, asserting which path ran | nothing |
| [04_http_request.py](04_http_request.py) | An HTTP node and the handling around it | network |
| [05_deploy_and_run.py](05_deploy_and_run.py) | Deploy to Dify, run against the real model, clean up | Dify + **costs money** |
| [06_code_node.py](06_code_node.py) | Run a code node stubbed, and locally confined by the OS | nothing |
| [07_agent_as_code.py](07_agent_as_code.py) | Keep a Dify Agent in version control: export, edit, redeploy | nothing |
| [08_webhook_trigger.py](08_webhook_trigger.py) | A workflow Dify starts itself, when a webhook arrives | Dify (no model) |

## What runs where

**01–03 need nothing at all** — no Dify server, no API key, no network. The
template, if-else and variable-aggregator nodes all execute locally, and
`StubLLM` stands in for the model. This is where workflow tests belong.

**04 reaches the network** but still no Dify: graphon has its own HTTP client.

**05 is the billed one.** It refuses to spend unless `DIFY_LIVE_TESTS` is set,
and prints what is missing instead:

```
stopping before the billed half: DIFY_LIVE_TESTS is not set; live runs call
real models and cost money
```

## Things worth copying

**Assemble prompts in a template node.** A prompt built inside its own node is
assertable without a model:

```python
assert result.node("prompt")["output"].startswith("Summarise the following")
```

**Join branches with a variable aggregator.** An answer that reads from two
branches renders the unexecuted one's reference as literal text, because that
variable was never produced. 03 shows the join.

**Assert the path, not just the output.** `result.nodes` holds only the nodes
that ran:

```python
assert "escalate" in result.nodes
assert "queue" not in result.nodes
```

**Declare the plugin of every model you use**, or Dify imports an app it cannot
run:

```python
wf.depends_on("langgenius/openai:0.3.8@592c8252795b…")
```

## Nodes without a typed helper

`start`, `template`, `llm`, `code`, `tool`, `answer` and `end` have helpers, as do
the `webhook` and `schedule` triggers that start a workflow in place of `start`.
Every other
node type goes through `wf.add()` with its graphon entity — 03 and 04 use it for
if-else, the variable aggregator and the HTTP node, and the same shape works for
iteration, tools, parameter extraction and the rest.

## The code node

A code node posts its program to `dify-sandbox`, so on its own it fails in a
local test. 06 shows the two ways round it — `StubCode` (do not run it) and
`LocalSandbox` (run it here, confined by the OS). Neither needs Docker. See
[Code nodes without Dify's sandbox](../README.md#code-nodes-without-difys-sandbox)
for what each one does and does not match.

## Triggers

A trigger node replaces the start node, so Dify runs the workflow rather than a
caller — on a schedule, or when a webhook arrives. Two things follow, and 08
shows both: the trigger is only real after `publish_workflow()`, and the node
types live in Dify rather than in graphon, so a workflow built around one does
not run locally. 08 keeps the body in one function and gives it two entry
points, testing through the start-node one.
