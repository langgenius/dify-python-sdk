# Nodes

## Typed helpers

| Helper | Node type | Notes |
|---|---|---|
| `wf.start(inputs)` | `start` | Input variables; helpers in `dify_client.workflow`: `text_input`, `paragraph`, `number`, `select`, `checkbox`, `file`, `file_list` |
| `wf.template(tpl, variables=)` | `template-transform` | Jinja2, runs locally with nothing installed |
| `wf.llm(prompt, model=)` | `llm` | `model` is `provider/plugin/name:model` |
| `wf.code(src, variables=, outputs=)` | `code` | Needs a sandbox — see `testing.md` |
| `wf.tool(spec, config=, params=)` | `tool` | `spec` comes from the workspace — see below |
| `wf.answer(text)` | `answer` | Makes the app a chatflow |
| `wf.end(outputs)` | `end` | Makes the app a workflow |
| `wf.knowledge(query, datasets)` | `knowledge-retrieval` | Stub it to run locally — see below |
| `wf.if_else(conditions)` | `if-else` | Conditions from `when(...)` |
| `wf.http(url, ...)` | `http-request` | Auth from `bearer`/`basic`/`api_key` |
| `wf.classify(query, classes, model=)` | `question-classifier` | Class id is the edge handle |
| `wf.extract_parameters(query, params, model=)` | `parameter-extractor` | Fields from `parameter(...)` |
| `wf.aggregate(variables)` | `variable-aggregator` | Rejoins branches |
| `wf.assign(assignments)` | `assigner` | Writes conversation variables |
| `wf.extract_text(files)` | `document-extractor` | Deploy-only |
| `wf.list_operator(variable, ...)` | `list-operator` | Filter, order, limit, extract |
| `wf.iteration(items)` | `iteration` | Context manager — see below |
| `wf.loop(count=)` | `loop` | Context manager; `body.until([...])` breaks |
| `wf.human_input(form, actions=)` | `human-input` | Deploy-only; each action is a branch |
| `wf.agent(strategy, parameters)` | `agent` | Deploy-only; strategy from a plugin |
| `wf.dify_agent(agent, task)` | `agent` v2 | Binds a published workspace Agent; app only |
| `wf.inline_agent(agent, task)` | `agent` v2 | Ships the Agent in the document; app only |
| `wf.datasource(...)` | `datasource` | Replaces `start`; pipeline node |
| `wf.knowledge_index(chunks)` | `knowledge-index` | Pipeline node |
| `wf.webhook(...)` | `trigger-webhook` | Replaces `start`; deploy-only — see below |
| `wf.schedule(cron=)` | `trigger-schedule` | Replaces `start`; deploy-only — see below |
| `wf.plugin_trigger(...)` | `trigger-plugin` | Replaces `start`; ids come from the server |

Anything else goes through `wf.add(entity, id=...)` with a graphon entity — or
one from `dify_client.workflow.nodes` for the types Dify implements
itself. The escape hatch is not second-class: it is how the helpers are built.

## What runs locally, and what only runs on Dify

`wf.run()` executes the document with graphon, which implements the engine, not
the server. The authoritative list is graphon's own, so read it rather than
trusting a copy:

```python
from graphon.dsl.node_factory import SUPPORTED_DEFAULT_FACTORY_NODE_TYPES
```

As of graphon 0.8: `start`, `end`, `answer`, `llm`, `code`,
`template-transform`, `http-request`, `if-else`, `iteration`, `loop`,
`list-operator`, `parameter-extractor`, `question-classifier`, `tool`,
`variable-aggregator`, `assigner`.

Everything else is Dify's own and has nothing to call locally:
`knowledge-retrieval`, `knowledge-index`, `document-extractor`, `human-input`,
`agent`, `datasource` and the three triggers. `wf.run()` says so and names the
type. The one with a local stand-in is the knowledge node:
`wf.run(inputs, knowledge=StubKnowledge([...]))`.

## Wiring

Most edges need not be written: a node that reads another node's output is
connected by that alone, and `to_dict()` derives those. `wf.connect` is for
control flow — which arm of a branch to take:

```python
branch = wf.if_else([when(start["message"], "contains", "urgent")])
wf.connect(branch.true, escalate)        # `branch.case("id")` for named cases
wf.connect(branch.false, queue)
wf.answer(wf.merge(escalate, queue))     # the aggregator that rejoins them
```

`wf.edges` lists what will be sent. Two rules keep it honest, and both were
bugs first:

1. **A node wired by hand is left alone.** An arm usually also reads something
   from before the branch; inferring an edge there would give it a path that
   skips the branch, so both arms run.
2. **Nothing is inferred across a container boundary.** A loop body may read an
   outer variable; that is a reference, not a step in the body.

Because a hand-written edge switches inference off for that node, it can
*replace* the edge a reference needed rather than adding to it — connecting a
chunker straight to its datasource leaves the extractor with nothing leading
to it. `validate()` refuses that: "these nodes read a node nothing leads to".

`if_else`, `classify` and `human_input` return a `Branch`, whose `.handles`
lists its arms (`("true", "false")`, the class ids, the action ids plus
`"__timeout"` — Dify's spelling, reached as `review.timeout`).

**Nothing is inferred out of a branch.** A branch decides which arm runs, and
an inferred edge has no arm, so Dify would never take it. A node that reads a
branch's output — `kind["class_name"]` — is wired by naming the arm:
`wf.connect(kind.case("billing"), node)`. `validate()` refuses the node
otherwise, and so does `wf.connect(branch, node)` with no arm.

## References between nodes

```python
llm["text"]          # {{#llm.text#}} in text, ["llm", "text"] as a selector
llm.output           # the conventional single output — "text" for llm
system.query         # {{#sys.query#}} — the chatflow's incoming message
wf.env_var("TOKEN", secret=True)   # returns {{#env.TOKEN#}}
```

`node.output` maps: `llm` → `text`, `http-request` → `body`,
`document-extractor` → `text`, `question-classifier` → `class_name`,
everything else → `output`.

## Branching

An if-else node plus a variable aggregator to rejoin. **The aggregator is not
optional**: without it, an answer reading from both branches renders the branch
that did not run as literal text, because that variable was never produced.

```python
from dify_client.workflow import when

branch = wf.if_else(
    [when(start["message"], "contains", word) for word in ("urgent", "outage")],
    logical="or", title="Urgent?", id="branch",
)
escalate = wf.template("PAGE — {{ m }}", variables={"m": start["message"]}, id="escalate")
queue    = wf.template("Queued — {{ m }}", variables={"m": start["message"]}, id="queue")
merged   = wf.aggregate([escalate.output, queue.output], id="merged")

wf.connect(start, branch)
wf.connect(branch, escalate, handle="true")     # handle = the case id
wf.connect(branch, queue, handle="false")       # "false" = the else branch
wf.connect(escalate, merged)
wf.connect(queue, merged)
wf.connect(merged, wf.answer(merged.output))
```

A mapping builds the ELIF chain, one case per key, and the keys are the handles:
`wf.if_else({"big": [...], "small": [...]})`, plus `"false"`.

`when()` checks the operator against graphon's own list, because Dify spells
them its own way (`≠`, not `!=`) and a wrong one imports fine and then never
fires: `contains`, `not contains`, `start with`, `end with`, `is`, `is not`,
`empty`, `not empty`, `in`, `not in`, `all of`, `=`, `≠`, `>`, `<`, `≥`, `≤`,
`null`, `not null`, `exists`, `not exists`.

### Asserting which path ran

`result.nodes` holds only the nodes that executed, so the route itself is
testable:

```python
assert "escalate" in result.nodes
assert "queue" not in result.nodes
```

## Tools

A tool node has to name a provider, a tool and every parameter with the right
kind, and those identifiers come from the workspace rather than from anything
worth typing by hand. Discovery is therefore an online step; authoring stays
offline once the catalogue is in hand.

```python
catalog = console.tools.catalog()                       # online, once

now  = wf.tool(catalog["time"]["current_time"],
               config={"timezone": "Asia/Tokyo", "format": "%Y-%m-%d %H:%M"}, id="now")
page = wf.tool(catalog["webscraper"]["webscraper"],
               params={"url": start["url"]},    # a reference becomes a variable input
               config={"generate_summary": False}, id="page")
```

**`config` and `params` are not interchangeable.** Dify marks each parameter
with a `form`, and putting one in the other's place produces a node Dify
accepts and then cannot run:

| `form` | Meaning | Argument |
|---|---|---|
| `form` | decided when the workflow is built | `config=` |
| `llm` | supplied per run | `params=` |

`wf.tool()` checks this against the spec and refuses a misplaced name:

```
'url' is supplied per run, so it belongs in params=, not config=.
Dify accepts the node either way and then cannot run it.
```

Inspect a tool before using it:

```python
spec = catalog["webscraper"]["webscraper"]
spec.configuration_names     # ['generate_summary', 'user_agent']
spec.runtime_names           # ['url']
spec.parameter("url").required
```

A tool node outputs **`text`, `files` and `json`** — not `output`. `node.output`
resolves to `text`.

Tools from a plugin declare that plugin automatically, so a deployed app has
what it needs; builtin providers declare nothing.

## Plugin identifiers

`depends_on` wants the identifier of the version actually installed. Declaring
a different one makes Dify try to fetch it on import, so read it rather than
copying a hash from a marketplace page:

```python
wf.depends_on(console.tools.identifier("langgenius/openai"))
console.tools.plugins()            # everything installed, with identifiers
```

## HTTP

Runs locally — graphon has its own client — so it reaches the network but needs
no Dify.

```python
from dify_client.workflow import api_key, basic, bearer

fetch = wf.http(
    "https://api.example.com/orders/{{#start.id#}}",
    method="get",
    headers={"Accept": "application/json"},
    params={"page": "1"},
    auth=bearer(wf.env_var("TOKEN", secret=True)),
    timeout=(10, 30, 30),          # connect, read, write
)
```

One body at most: `json=` (a mapping is serialised), `text=`, or `form=`.
Headers and query parameters are stored as one `name: value` per line, which is
what Dify parses; pass a mapping and the builder writes that form.

Outputs: `status_code`, `body`, `headers`, `files`.

## Iteration and loop

Both are containers: the nodes built inside the `with` block belong to them,
which is what the `parentId` in the DSL says and what makes Dify draw them
inside the box.

```python
with wf.iteration(source["items"]) as each:
    greet = wf.template("Hi {{ n }}", variables={"n": each.item})
    each.returns(greet.output)              # what this pass contributes

wf.connect(start, source, each, wf.end({"all": each.output}))
```

`each.item` is the current element, `each.index` its position, and the node's
own `output` is the array of everything returned. `on_error=` is Dify's own:
`terminated`, `continue-on-error`, `remove-abnormal-output`.

A loop repeats instead of walking an array, and carries variables between
passes:

```python
from dify_client.workflow import loop_var, when

with wf.loop(count=5, variables=[loop_var("tally", 0, type="number")]) as body:
    bump = wf.code("def main(n): return {'n': n + 1}",
                   variables={"n": body.var("tally")}, outputs={"n": "number"})
    wf.connect(bump, wf.assign([(body.var("tally"), "over-write", bump["n"])]))
    body.until([when(bump["n"], "≥", 5)])
```

The body's nodes are connected to each other as usual; the container wires its
own start marker into the first one.

**Where a break condition goes.** `body.until([...])` is set from inside the
block, because that is the only point at which a condition can name a node in
the body — the `until=` argument on `wf.loop(...)` is evaluated before the
block runs, so it can only name something outside the loop. `count` bounds the
loop whichever is used.

Both are checked between passes. To leave during one, connect to a loop-end
node: `wf.connect(check, body.stop(), handle="true")`.

## Knowledge retrieval

```python
hits = wf.knowledge(start["q"], [DATASET_ID], top_k=3, rerank=RERANK_MODEL)
reply = wf.llm([("system", f"Answer only from: {hits.output}"),
                ("user", start["q"])], model=MODEL)
```

`result` is an array of objects with `content`, `title` and `metadata`;
`hits.output` refers to it. Dataset ids come from
`DifyKnowledge.datasets.list()`.

`mode="single"` has a model choose one base first and needs `model=`;
`mode="multiple"` (the default) searches all of them and `rerank=` scores the
merge.

Retrieval lives in the server, so a local run needs a stand-in:

```python
from dify_client.workflow import Chunk, StubKnowledge

know = StubKnowledge(["Refunds take 5 business days."])
result = wf.run({"q": "refund?"}, knowledge=know)
assert know.calls[0].query == "refund?"
```

`StubKnowledge` also takes `{dataset_id: [...]}` to prove which base was asked,
or a callable of `(query, dataset_ids)`. `Chunk(text, score=…)` adds scores,
and scored chunks come back best first. **This is a fixture, not a claim about
retrieval quality** — that is a property of the server's index, so assert it
with `wf.run_live()` or `DifyKnowledge.datasets.search()`.

**Dify encrypts `dataset_ids` on export**, keyed by the workspace. An exported
DSL therefore does not name the knowledge base, and importing that export into
another workspace drops the id silently — leaving a knowledge node with no
knowledge base. Keep the builder code, not an export, as the source of truth.

## Human input

```python
from dify_client.workflow import action, form_paragraph, form_select

review = wf.human_input(
    "Approve this draft?\n\n{{#$output.note#}}",
    inputs=[form_paragraph("note"), form_select("reason", ["typo", "tone"])],
    actions=[action("approve", "Approve", style="primary"),
             action("reject", "Reject")],
    timeout=3, timeout_unit="day",
)
wf.connect(review, ship, handle="approve")
wf.connect(review, stop, handle="reject")
```

Each action is a branch handled by its id, the form's fields become the node's
outputs, and `{{#$output.field#}}` in the markdown renders one. Dify does the
waiting, so this is deploy-only.

A dropdown takes a fixed list or a reference to an `array[string]` output, so
it can offer what the run just found:

```python
form_select("reason", ["typo", "tone"])     # fixed
form_select("topic", listed["topics"])      # built when the form is shown
```

## Agents

Three nodes, and the differences matter.

**A plugin strategy** — Dify's first agent node:

```python
think = wf.agent("langgenius/agent/function_calling", {"query": start["q"]},
                 plugin=console.tools.identifier("langgenius/agent"))
```

Dify resolves the strategy when the node *runs*, so publishing succeeds even
when the plugin is missing; `plugin=` is what makes the deployed app install it.

**A published workspace Agent** — Dify's second agent node, bound to the roster:

```python
triage = console.agents.retrieve("support-triage")
node = wf.dify_agent(triage, "Say whether this pages someone.",
                     outputs=[declared_output("severity")])
```

The Agent is shared: other workflows may bind the same one, and republishing it
changes what they all run. It must be published before the import — Dify writes
the binding record while it writes the draft, so an unpublished Agent fails the
*import*, not the publish.

**An Agent shipped inside the workflow**, which is the self-contained route:

```python
from dify_client import Agent

researcher = Agent.create("researcher", instruction="…", model=MODEL)
node = wf.inline_agent(researcher, "Summarise what you find.")
```

The Agent travels in the document under `agent_packages`, and Dify creates one
owned by this node on import. Nothing outside the file can change what the node
runs, and the workflow carries its Agent to another workspace. The trade is that
it is not the workspace's Agent: roster edits do not reach it, and it does not
appear on the roster. Its soul is blanked by `to_yaml()` the way a secret
environment variable is.

`declared_output(name, type)` names a field the agent must answer with. Both
routes are deploy-only: an agent runs in Dify.

**The last two are an app's, not a pipeline's.** Dify writes the binding record
while it imports an *app* draft; the pipeline importer does neither, accepts the
document, publishes it and drops `agent_packages` — leaving a node bound to
nothing. `wf.agent()`, whose strategy is resolved at run time, works in both.

**Three ways an agent node fails, in three different places.** An unpublished
roster Agent and an empty `agent_binding` both fail the *import*; a binding
that names a kind but no id is skipped on import and fails the *publish* with
"requires a binding before publishing". The helpers refuse the last two before
anything is sent.

## Knowledge pipelines

`datasource` and `knowledge-index` are the two ends of a knowledge pipeline —
where documents come from, and the chunks written back. `Pipeline` writes the
document they belong in; an app workflow carrying one imports and publishes
too (checked against 1.17.1).

```python
from dify_client.workflow import Pipeline

pipe = Pipeline("support-docs")
files = pipe.datasource(plugin_id="langgenius/file", provider="file",
                        name="upload-file")
text  = pipe.extract_text(files["file"])
index = pipe.knowledge_index(text.output)          # text_model + economy by default
pipe.connect(files, text, index)

result = console.pipelines.deploy(pipe)
result.pipeline_id, result.dataset_id
```

`pipe.variable(files, "source_url", label="URL")` declares an input the
pipeline asks for. It belongs to a datasource, is referenced as
`{{#rag.<node>.<name>#}}` — three parts, not two — and becomes a form field in
Dify's UI. `pipe.variable(None, "chunk_size")` puts one in the `shared` scope
instead (`{{#rag.shared.chunk_size#}}`), which is where Dify's own templates
keep the settings every datasource uses.

**A knowledge-index node takes chunks, not text.** An extractor wired straight
into it queues the document and then fails indexing. The chain Dify's templates
use is `datasource → dify_extractor → general_chunker → knowledge-index`, and
the two middle nodes are `wf.tool(catalog[...])` — marketplace plugins, so a
workspace without them cannot index through a pipeline at all.

Running one is the knowledge client's job, not the console's:

```python
runner = knowledge.pipeline(result.dataset_id)
(source,) = runner.datasources()                 # the node ids in the graph
uploaded  = knowledge.upload_for_pipeline("handbook.pdf")
queued    = runner.run(start_node_id=source["node_id"],
                       datasource_type="local_file",
                       datasource_info_list=[{"reference": uploaded["id"]}])
knowledge.documents(dataset).wait_until_indexed(queued.batch)
```

A published run **queues**: it answers with a batch as soon as the documents
are enqueued, so there is nothing to stream. `run_draft()` / `stream_draft()`
execute the unpublished graph instead and report a `WorkflowRun`.

**Five things a pipeline does not share with an app.**

1. Its DSL is `kind: rag_pipeline` at version **0.1.0**. Sending the app's
   `0.7.0` makes Dify hold the import for confirmation.
2. The import payload's `name` is accepted and never read. The knowledge base
   is named from the document.
3. That name always gets a number appended — `support-docs` fills
   `support-docs 1` — so address the base by the `dataset_id` the deploy
   returns.
4. `knowledge_index` must carry search settings (`retrieval_model`); the
   builder writes a default matching `indexing`, because without them the
   import fails on a field the DSL never mentions.
5. There is no route that deletes a pipeline: `console.pipelines.delete(result)`
   deletes the knowledge base, and the pipeline with it.

`indexing="high_quality"` embeds every chunk, so it needs
`embedding="provider/plugin/name:model"`; `economy` is keyword search and needs
no model. Chunk structures: `text_model`, `hierarchical_model`, `qa_model`.

How the base will be searched is set here too, and it sticks: every later
retrieval reads it, a workflow's knowledge node included.

```python
pipe.knowledge_index(
    chunks.output,
    indexing="high_quality",
    embedding="langgenius/openai/openai:text-embedding-3-small",
    search="hybrid_search",            # semantic / full_text / hybrid / keyword
    top_k=5,
    score_threshold=0.2,               # also turns on the flag that reads it
    rerank="langgenius/cohere/cohere:rerank-v3.5",
)
```

`rerank=` scores with a model; `weights=weighted_score(embedding=…)` blends the
vector and keyword scores instead and calls none. One or the other. The same
settings apply to a knowledge base made directly:
`knowledge.datasets.create(name, embedding=…, retrieval=retrieval_model(...))`.

A pipeline cannot run locally — both of its ends are the server — and
`pipe.run()` says so.

## Triggers

A trigger node replaces the start node: Dify starts the run rather than a
caller. Two kinds.

```python
from dify_client.workflow import body_field, header, query_param

hook = wf.webhook(
    method="post",                                  # default; lowercased for Dify
    headers=[header("x-signature", required=True)],
    params=[query_param("page", "number")],
    body=[body_field("order_id", required=True), body_field("total", "number")],
)
wf.connect(hook, wf.end({"id": hook["order_id"]}))
```

Each declared field is an output of the node, under its own name, so
`hook["order_id"]` is the posted value. Types are limited per surface and
checked here rather than at import:

| Surface | Allowed types |
|---|---|
| `header` | `string` only |
| `query_param` | `string`, `number`, `boolean` |
| `body_field` | those plus `object`, `array[...]`, `file` |

```python
wf.schedule("0 2 * * *", timezone="Asia/Tokyo")                     # cron mode
wf.schedule(frequency="weekly", at={"time": "9:00 AM", "weekdays": ["mon"]})
```

`frequency` is one of `hourly`, `daily`, `weekly`, `monthly`, and `at` carries
the detail (`on_minute`, `time`, `weekdays`, `monthly_days`). Both modes are the
same trigger to Dify; visual mode is the one the editor can render as a form.

**Two things to know.**

1. **A trigger is not real until publish.** Import writes a draft, and a trigger
   node in a draft is only a drawing. `console.apps.publish(app_id)`
   materializes the trigger and mints the webhook's URL. Read it back with
   `console.apps.triggers.list(app_id)` and
   `console.apps.triggers.webhook(app_id, "trigger_webhook")`;
   `console.apps.triggers.set_enabled(app_id, trigger_id, enabled=False)` pauses one.

2. **They do not run locally.** These node types live in Dify's own code, not in
   graphon, so `wf.run()` has nothing to call. Build and test the graph behind a
   `wf.start(...)`, then swap the trigger in for deployment.

## Environment variables

```python
token = wf.env_var("API_TOKEN", "value", secret=True)   # {{#env.API_TOKEN#}}
```

Secret ones are blanked by `to_yaml()` and kept for local runs, so the exported
file is safe to commit. Set their real values once in the Dify UI; later deploys
leave them alone.

## Layout

Canvas positions are generated left-to-right by distance from the start node, so
an imported app is readable rather than stacked at the origin. Nothing to
configure.

## Finding an entity's shape

The schemas are pydantic models in the installed `graphon`, so read them
directly rather than guessing:

```python
from graphon.nodes.parameter_extractor.entities import ParameterExtractorNodeData
for name, field in ParameterExtractorNodeData.model_fields.items():
    print(name, field.annotation, field.is_required())
```

The node data Dify owns and graphon does not is restated in
`dify_client.workflow.nodes`, one module per family — `agents`,
`human_input`, `knowledge`, `logic`, `http`, `models`, `tools`, `triggers`.
Each also holds what Dify accepts of that node type, so a refusal like "mode
'single' needs a model" is next to the schema it is about rather than in the
builder. `dify_client.workflow.local_knowledge` is the stand-in that runs a
knowledge node without a server. Which types *run* locally is the list above, and it comes
from graphon, not from this file.
