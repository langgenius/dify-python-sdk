---
name: Dify Workflow
description: This skill should be used when the user asks to "build a Dify workflow", "create a Dify app in code", "deploy a workflow to Dify", "test a Dify workflow", "write a chatflow", "import a DSL into Dify", "export a Dify agent", or mentions dify-client, the Dify DSL, or managing Dify apps as code. Covers defining workflows with the Python builder, testing them offline, and deploying them to a Dify instance.
version: 0.1.0
---

# Dify workflows as code

Define a Dify workflow in Python, test it without a Dify server, then deploy it.
The node schemas come from `graphon` — the engine Dify itself runs — so a
workflow built here is checked against the server's own definitions.

Install with `pip install "dify-client[workflow]"`. Requires Python 3.12 or 3.13.

## The loop

Work in three stages, and do not skip the middle one:

```python
from dify_client import DifyManagement
from dify_client.workflow import StubLLM, Workflow, paragraph, select

# 1. Define
wf = Workflow("tone-rewriter")
wf.depends_on("langgenius/openai:0.3.8@592c8252795b…")   # required, see below
start = wf.start([paragraph("draft"), select("tone", ["formal", "casual"])])
prompt = wf.template(
    "Rewrite in a {{ tone }} tone.\n\n{{ draft }}",
    variables={"draft": start["draft"], "tone": start["tone"]}, id="prompt")
llm = wf.llm(prompt.output, model="langgenius/openai/openai:gpt-4o-mini", id="llm")
wf.connect(start, prompt, llm, wf.answer(llm.output))

# 2. Test — free, offline, no Dify and no API key
result = wf.run({"draft": "we ship tomorrow", "tone": "formal"}, llm=StubLLM("canned"))
assert "formal tone" in str(result.node("prompt")["output"])
assert not result.usage          # a stubbed run costs nothing

# 3. Deploy
console = DifyManagement.login(email, password, base_url=host)
imported = console.apps.deploy(wf)
console.apps.publish(imported.app_id)        # NOT optional — see below
```

Stage 2 is where prompt bugs are caught for free. `StubLLM` answers every LLM
node; `result.node(id)` reaches any node's inputs and outputs, so assert on what
the workflow *assembled*, not just its final text.

## Five things that bite

**1. `answer` versus `end` decides everything downstream.** An `answer` node
makes the app a chatflow (`advanced-chat`); an `end` node makes it a `workflow`.
The Service API serves them at different paths and rejects the wrong one. A
chatflow also needs a `query` at run time **and** its start inputs:

```python
app.workflows.runs.create({"message": text}, query=text)   # chatflow: both
app.workflows.runs.create({"name": "Dify"})                # workflow: inputs alone
```

**2. Join branches with a variable aggregator.** An answer that reads from two
branches renders the unexecuted branch's reference as literal text, because that
variable was never produced. Route both into a `VariableAggregatorNodeData` and
read its `output`. See `references/nodes.md`.

**3. Declare the plugin of every model.** Dify installs a workflow's declared
plugins on import; an undeclared provider deploys into an app that cannot run.
Read the identifier of the installed version rather than copying a hash —
`console.tools.identifier("langgenius/openai")` — and check with
`wf.missing_plugin_dependencies()`. Tool nodes declare their own plugin.

**4. Importing writes a draft.** The Service API runs only published work, so
`deploy()` alone leaves an app that answers *workflow not published*. Call
`console.apps.publish(app_id)`. The Dify UI hides this because its Publish
button does both.

**5. Code nodes need a sandbox.** `wf.code(...)` posts to `dify-sandbox` and
fails locally. Pass `code=LocalSandbox()` to run it here instead, confined by
the OS. Name what the deployment has: `LocalSandbox(packages=["httpx"])` — the
default hides the project's virtualenv, because a node that can `import pandas`
locally and not in production is a test that lies.

## Reaching the message and other system variables

A chatflow's incoming message is `sys.query`, and no node produces it:

```python
from dify_client.workflow import system
wf.template("You said {{ q }}", variables={"q": system.query})
```

`system.user_id`, `system.conversation_id` and any other name work the same way.

## Node helpers

Typed helpers: `start`, `template`, `llm`, `code`, `tool`, `answer`, `end`, plus
`webhook` and `schedule`, which start a workflow in place of `start` — those two
only run on a deployed Dify, never locally. Every other node type goes through
`wf.add()` with its graphon entity:

```python
from graphon.nodes.if_else.entities import IfElseNodeData
branch = wf.add(IfElseNodeData(title="Urgent?", cases=[...]), id="branch")
wf.connect(branch, escalate, handle="urgent")   # handle = case id, or "false"
```

Indexing a node gives a reference that renders both ways Dify needs —
`node["field"]` is `{{#node.field#}}` in text and `["node", "field"]` as a
selector. `node.output` is the conventional single output (`llm.output` is
`llm["text"]`).

## Deploying

Two APIs exist. `DifyManagement` works against any Dify. `OpenApiClient`
(`/openapi/v1`) is the sanctioned programmatic surface with scoped tokens, but
is off by default and cannot publish. Check with `OpenApiClient.available()`.
Details and credentials in `references/deploying.md`.

Secrets never reach an exported DSL: `wf.to_yaml()` blanks secret environment
variables, and model credentials are an argument to the run, never serialised.
The file is safe to commit.

## Before deploying, check

- `wf.validate()` passes (called by `to_yaml()` anyway)
- `wf.missing_plugin_dependencies() == []`
- A stubbed `wf.run(...)` succeeds and asserts what matters
- The mode is what was intended: `wf.mode`

## Additional resources

- **`references/nodes.md`** — the node catalogue, entity shapes for nodes
  without helpers, branching and iteration patterns
- **`references/testing.md`** — `StubLLM`, `StubCode`, `LocalSandbox`, usage and
  cost accounting, and billed tests against a real instance
- **`references/deploying.md`** — `DifyManagement` vs `OpenApiClient`, credential
  model and environment variables, building and editing Agents, Agent skills,
  ephemeral apps

Runnable scripts live in the SDK's `examples/` directory; `01`–`03` need nothing
installed beyond the SDK.
