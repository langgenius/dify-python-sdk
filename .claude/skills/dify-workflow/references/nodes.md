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
| `wf.webhook(...)` | `trigger-webhook` | Replaces `start`; deploy-only — see below |
| `wf.schedule(cron=)` | `trigger-schedule` | Replaces `start`; deploy-only — see below |

Everything else goes through `wf.add(entity, id=...)` with a graphon entity.
The escape hatch is not second-class: it is how the helpers are implemented, and
every node type graphon supports works through it.

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
from graphon.nodes.if_else.entities import IfElseNodeData
from graphon.nodes.variable_aggregator.entities import VariableAggregatorNodeData
from graphon.utils.condition.entities import Condition

branch = wf.add(IfElseNodeData(
    title="Urgent?",
    cases=[IfElseNodeData.Case(
        case_id="urgent",
        logical_operator="or",
        conditions=[Condition(variable_selector=start["message"].selector,
                              comparison_operator="contains", value=word)
                    for word in ("urgent", "outage", "down")],
    )],
), id="branch")

escalate = wf.template("PAGE — {{ m }}", variables={"m": start["message"]}, id="escalate")
queue    = wf.template("Queued — {{ m }}", variables={"m": start["message"]}, id="queue")

merged = wf.add(VariableAggregatorNodeData(
    title="Merge", output_type="string",
    variables=[escalate.output.selector, queue.output.selector],
), id="merged")

wf.connect(start, branch)
wf.connect(branch, escalate, handle="urgent")   # handle = the case id
wf.connect(branch, queue, handle="false")       # "false" = the else branch
wf.connect(escalate, merged)
wf.connect(queue, merged)
wf.connect(merged, wf.answer(merged["output"]))
```

Comparison operators: `contains`, `not contains`, `start with`, `end with`,
`is`, `is not`, `empty`, `not empty`, `in`, `not in`, `all of`, `=`, `≠`, `>`,
`<`, `≥`, `≤`, `null`, `not null`, `exists`, `not exists`.

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
from graphon.nodes.http_request.entities import (
    HttpRequestNodeAuthorization, HttpRequestNodeData)

fetch = wf.add(HttpRequestNodeData(
    title="Fetch", method="get", url=str(start["url"]),
    authorization=HttpRequestNodeAuthorization(type="no-auth"),
    headers="", params="",
), id="fetch")
```

Outputs: `status_code`, `body`, `headers`, `files`.

## Iteration

```python
from graphon.nodes.iteration.entities import IterationNodeData

loop = wf.add(IterationNodeData(
    title="Each", start_node_id="inner_start",
    iterator_selector=source["items"].selector,
    output_selector=inner_last.output.selector,
), id="loop")
```

Iteration and loop nodes carry their own sub-graph; the nodes inside are added
to the same workflow and referenced by id.

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

Node types graphon supports: `start`, `end`, `answer`, `llm`, `code`,
`template-transform`, `http-request`, `if-else`, `iteration`, `loop`,
`list-operator`, `parameter-extractor`, `question-classifier`, `tool`,
`variable-aggregator`, `variable-assigner`, `document-extractor`,
`human-input`, `agent`, `knowledge-retrieval`.
