# dify-client

[Changelog and migration guide](CHANGELOG.md)

A Dify App Service-API Client, using for build a webapp by request Service-API

It also keeps Dify apps in version control: [workflows](#workflows-as-code)
defined and tested in Python, and [agents](#agents-as-code) exported, edited and
redeployed. For key handling,
see [API keys and credentials](#api-keys-and-credentials).

## Start here

```python
from dify_client import DifyApp

app = DifyApp(api_key="app-…", user="alice")

message = app.chat.messages.create("Hello")
print(message.answer, message.conversation_id)

run = app.workflows.runs.create({"text": "…"})
print(run.status, run.outputs, run.usage)
```

A client holds the connection and the credential. What you can do hangs off
what it acts on, so everything about one kind of thing is in one place:

| | |
|---|---|
| `app.chat.messages` | `create`, `stream`, `list`, `stop`, `feedback`, `suggested` |
| | `create` returns a `Message`; `list` returns a `Page[HistoryMessage]`, which keeps the question each answer replied to |
| `app.chat.conversations` | `list`, `rename`, `delete`, `variables` |
| `app.workflows.runs` | `create`, `stream`, `retrieve`, `events`, `stop`, `logs` |
| `app.completions` | `create`, `stream`, `stop` |
| `app.files` | `upload`, `download` |
| `app.annotations` | `list`, `create`, `update`, `delete`, `set_reply` |
| `app.audio` | `speak`, `transcribe` |
| `app.forms` | `retrieve`, `submit` |

Calls return the thing, not an HTTP response, carrying the ids the next call
needs:

```python
run.run_id      # reads the run back later
run.task_id     # what a stop request names — a different id, kept separate
run.status      # succeeded / failed / paused are three separate questions
run.outputs     # what the workflow produced
run.usage       # tokens and cost, summed over every node execution
```

### What an app takes

`parameters()` answers what a run is allowed to be given. Dify sends the form
as a list of single-key objects (`{"text-input": {…}}`), which nobody should
have to unwrap to find out whether a field is required:

```python
parameters = app.parameters()

for field in parameters:
    print(field.name, field.type, field.required)   # name is the key you pass

parameters["tone"].options        # ("short", "long")
parameters.required               # what a run is rejected without
parameters.features["speech_to_text"]
parameters.payload                # everything Dify sent, untouched
```

The same applies to `app.site()` (the WebApp settings) and
`knowledge.models(...)`, where a model carries the provider it belongs to —
Dify leaves that out of the model's own payload, and every call that takes a
model name takes a provider beside it:

```python
for provider in knowledge.models("text-embedding"):
    for model in provider:
        print(model.provider, model.name, model.usable)
```

Where Dify's shape is open-ended and keeps growing — `app.meta()`, a run log, a
datasource plugin's descriptor — the answer is still a `dict`. Naming those
fields would be a promise this SDK cannot keep. Everything typed carries the
raw answer on `.payload`, so a field a newer Dify adds is never lost.

### Is this host a Dify?

```python
DifyApp.probe("https://dify.example/v1")
# ServerInfo(server_version='1.17.1', api_version='v1', welcome='Dify OpenAPI')
```

No credential, and the one place the running version is reliably reported. Worth
calling first: an unreachable host and a wrong key look the same once
authenticated calls start.

### The three ways in

Dify scopes three credentials, and the entry points follow that rather than
hiding it — a Service-API key cannot create an app, and no API design makes it
able to:

| Entry point | Credential | For |
|---|---|---|
| `DifyApp` | an app's key (`app-…`) | running one app, its conversations, its files |
| `DifyKnowledge` | a dataset key | datasets, documents, retrieval |
| `DifyManagement` | a console session | creating apps, publishing, minting keys |

```python
knowledge = DifyKnowledge(api_key="dataset-…")
dataset = knowledge.datasets.create("handbook")
docs = knowledge.documents(dataset)
doc = docs.create(text="…", name="policy")
docs.wait_until_indexed(doc)                 # not searchable before this

for hit in knowledge.datasets.search(dataset, "what is the refund window?"):
    print(hit.score, hit.segment.content)
```

`knowledge.models()` is here rather than on `DifyApp` because Dify guards that
route with the dataset token — the one Service-API route that does.

`knowledge.tags` covers the workspace's tags, `knowledge.pipeline(dataset)` a
knowledge base's RAG pipeline where it has one, and `knowledge.documents(dataset)
.segments(document)` the chunks beneath a document.

A pipeline does two different things behind one route, and the return types say
which: `pipeline.run(...)` runs the **published** pipeline, which is queued —
it answers with the batch of documents it enqueued, not with the work — while
`run_draft(...)` and `stream_draft(...)` execute the *draft* graph and report a
`WorkflowRun`, the same type `app.workflows.runs` returns.

```python
queued = knowledge.pipeline(dataset).run(
    start_node_id="start",
    datasource_type="local_file",
    datasource_info_list=[{"file_id": file_id}],
)
docs.wait_until_indexed(queued.batch)        # nothing is indexed yet
```

### Listings

Everything that lists returns a `Page`, whichever way Dify pages it — some
listings take a `page` number, others a cursor, a few answer with a bare array.
The difference stays visible in the arguments and disappears from what you do
with the result:

```python
page = app.chat.conversations.list()
for thread in page:              # this page, already fetched
    ...
for thread in page.all():        # every page, fetched as it goes
    ...
page.has_more, page.total        # what Dify said, not a guess
```

`all()` walks a loop the *server* controls, so it stops at 10,000 items and
raises `PageLimitReached` rather than fetching forever — the error carries what
it read, and `all(max_items=…)` raises the ceiling when a listing really is
that long. A cursor that comes back unchanged ends the walk too: asking again
would return the same page.

### Watching a run happen

A stream is how you observe a run, not a different kind of call:

```python
with app.workflows.runs.stream({"text": "…"}) as stream:
    for event in stream:
        if event.type == "node_finished":
            print(event.execution.node_id, event.execution.usage)
    run = stream.get_final_run()

with app.chat.messages.stream("Hello") as stream:
    for piece in stream.text():
        print(piece, end="", flush=True)
    message = stream.get_final_message()
```

Four things Dify keeps apart, and so does this:

* **Closing the stream** stops watching. The run keeps going — stop it with
  `app.workflows.runs.stop(run)`.
* **A pause** is the run waiting for a person. `run.paused` is true while
  `succeeded` and `failed` are both false.
* **A dropped connection** says nothing about the run. Reopen it with
  `app.workflows.runs.events(run.run_id)`.
* **An HTTP 200** is not a successful run. Dify reports a mid-stream failure as
  an `error` event after the status line has gone out; the stream raises for it.
* **A stream that stopped** is not a finished one. `message.finished` is set
  only when Dify says the message is done, so a truncated answer does not pass
  for a complete one. `stream.snapshot()` is the answer so far;
  `get_final_message()` is the answer.

Every event keeps its whole payload — `event.payload`, or `event["whatever"]` —
so a field a newer Dify adds still reaches you.

`run.usage` is what Dify said the whole run cost, which is not always what this
client watched: reopening a finished run delivers the total and no node events
at all. `run.node_usage` is the part that was observed, and the difference is
what happened while nobody was looking.

### Waiting for a person

```python
run = app.workflows.runs.create(inputs)
if run.paused:
    form = app.forms.retrieve(run)
    print(form.content, form.actions)
    app.forms.submit(run, {"decision": "approve"}, action=form.actions[0]["id"])
    with app.workflows.runs.events(run.run_id) as stream:
        list(stream)
        run = stream.get_final_run()
```

### From a definition to a running app

Import, publish and run are three things on Dify, and a name that blurs them
hides which one a call did. Importing writes a **draft**; the Service API runs
the **published** version:

```python
from dify_client import DifyManagement, Stage

management = DifyManagement.login(email, password)

drafted = management.apps.import_definition(workflow)   # -> Stage.DRAFTED
management.apps.publish(drafted.app_id)                 # -> Stage.PUBLISHED

result = management.apps.deploy(workflow)               # all of it at once
result.raise_for_stage()                                # or inspect result.stage
app = management.apps.open(result.app_id).client()
```

`deploy` reports what happened rather than raising, because the middle states
are real: an app that imported but would not publish still exists, and the
caller has to know that before deciding to retry or delete it.

Three independent facts, not one ladder — squashing them let a later one stand
in for an earlier one, so a failed import over an existing app reported
`runnable`:

```python
result.imported        # a draft exists on Dify
result.published       # a version is live
result.has_key         # there is a key to call it with
result.indeterminate   # the request went out and nothing came back
result.created         # this call created the app — what makes deleting it safe
result.stage           # a one-word summary of the above, for printing
result.error
```

`api_key` is on the result but out of its `repr`, so printing a deploy does not
put a key in a log.

`management.apps.temporary(workflow)` deploys for the block and deletes after,
including when the deploy itself failed partway.

Management is grouped the same way: `management.apps`, `.apps.keys`,
`.apps.triggers`, `.skills`, `.agents`, `.models`, `.tools`.

### Async

Same shape throughout, with `await` in front of anything that sends. Every verb
on `DifyApp` and `DifyKnowledge` has a counterpart — a test asserts it, because
async used to be a subset:

```python
async with AsyncDifyApp(api_key, user="alice") as app:
    message = await app.chat.messages.create("Hello")

    stream = await app.chat.messages.stream("Hello")
    async for piece in stream.text():
        print(piece, end="", flush=True)
    message = stream.get_final_message()

async with AsyncDifyKnowledge(api_key="dataset-…") as knowledge:
    dataset = await knowledge.datasets.create("handbook")
    docs = knowledge.documents(dataset)      # picks a resource, sends nothing
    doc = await docs.create(text="…", name="policy")
    await docs.wait_until_indexed(doc)       # gives the loop back while it waits

    page = await knowledge.datasets.list()
    async for one in page.all():
        ...
```

A listing there is an `AsyncPage`: the same fields and the same verbs, awaited.
It is a separate type on purpose — a single one whose `next_page()` sometimes
blocked and sometimes returned a coroutine would be a trap in whichever context
it was not written for.

### Errors

```
DifyClientError                 everything below
├── APIError                    Dify answered, and it was an error
│   ├── AuthenticationError     401
│   ├── RateLimitError          429, with .retry_after
│   ├── ValidationError         422, and arguments rejected before sending
│   └── FileUploadError         an upload Dify would not take
└── TransportError              the request never got an answer
    ├── NetworkError            the connection failed
    └── RequestTimeout          it ran past the timeout
```

Every error Dify answers carries the server it came from, because the first
question about a failing call is always which Dify and which version:

```python
except APIError as error:
    error.message         # "Access token is invalid"
    error.server_version  # "1.17.1", from the X-Version header
    error.server_env      # "PRODUCTION"
    error.trace_id        # set when Dify is tracing
    str(error)            # "Access token is invalid [Dify 1.17.1]"
```

### Retries, rate limits and timeouts

A failure *before* the request was sent is always retried. A failure after it
was sent is retried only for an idempotent method — repeating a timed-out
`POST /workflows/run` can bill the same run four times.

A 429 is different: Dify refused the request, so there is nothing it might
already have done. That one is waited out and repeated for any method,
honouring `Retry-After` (a delay or a date), up to `max_retries`. A server
asking for longer than a minute is not waited for — the call raises
`RateLimitError` with `.retry_after` rather than blocking for an hour.

One timeout cannot serve both a conversation read and a workflow that runs for
twenty minutes, so it can be scoped to a block instead of threaded through
eighty signatures:

```python
with app.with_timeout(600):
    run = app.workflows.runs.create(inputs)
```

It applies to the calling thread or task only, so raising it for one long call
does not raise it for everything else in flight.

`timeout=` and `http_client=` together is refused rather than ignored: a
request made through your own client uses that client's timeout, so the number
passed here would have done nothing. Set it on the client, or scope it with
`with_timeout`.

### Bringing your own httpx client

Every client takes `http_client=`, for a proxy, a corporate TLS bundle, a
shared connection pool, or a transport that never leaves the test process. The
package ships `py.typed`, so its annotations reach mypy and pyright.


## Every route, and the call that reaches it

The SDK covers the whole Service API. That is checked rather than asserted:
`tests/test_service_api_coverage.py` reads the routes out of Dify's own
controllers with `ast` and fails if one has no call reaching it, verb by verb.
It also reads which routes *and which payload fields* Dify marks deprecated, so
"the SDK calls nothing deprecated" is checked rather than remembered. The Dify
it checks against is pinned in `tests/DIFY_VERSION`, and CI clones that tag —
otherwise the check would skip, and a skipped check proves nothing.

It exists because the coverage was checked by hand twice and the second rewrite
still dropped eleven routes — tags, the RAG pipeline, half the metadata, file
download, pinning a run to a published version. A hand check does not survive a
refactor.

Five routes are deliberately uncovered, and the test names each with its reason.
Every one is a route **Dify itself deprecates or duplicates**, never one that is
merely inconvenient:

| Route | Why |
|---|---|
| `/datasets/<id>/document/create_by_text`, `create_by_file` | Dify's `DeprecatedDocumentAddByTextApi` and friends. The hyphenated spellings are canonical, and those are what the SDK calls |
| `/datasets/<id>/documents/<id>/update_by_text` | same, deprecated |
| `/datasets/<id>/documents/<id>/update_by_file`, `update-by-file` | *both* spellings are deprecated; `PATCH /documents/<id>` replaces them |
| `/datasets/<id>/hit-testing` | the same Resource as `/retrieve`, which `datasets.search` calls |


## Checking what a Dify supports

The version comes from `GET /v1/` — the Service API's index, no credential
needed. But two servers on the same version do not agree, because self-hosted
deployments turn features off individually. So ask rather than infer:

```python
from dify_client import DifyManagement, probe

management = DifyManagement.login(email, password)
compat = probe(management=management)

print(compat.summary())
compat.require("triggers")      # raises, naming what is missing and why
```

```
Dify at http://localhost — 1.17.1 COMMUNITY
  SDK writes DSL 0.7.0
  agents: yes
  console_csrf: yes (this session carries one)
  human_input: yes
  openapi: yes
  triggers: yes
```

Each answer is one of three things — available, absent, or **not determined**,
because probing it needed a credential that was not supplied. Not knowing is
not permission to proceed, so `require()` refuses on it too.

This is a pre-flight worth running before deploying: a workflow with a trigger
node imports cleanly into a Dify without triggers, and then never starts.

## The live harness

`tests/live/` holds contract tests against a running Dify — the checks that
were done by hand while this SDK was built, kept so the next Dify release
cannot break them quietly.

```bash
export DIFY_HOST=http://localhost
export DIFY_CONSOLE_EMAIL=… DIFY_CONSOLE_PASSWORD=…

pytest tests/live        # the harness
pytest -m "not live"     # everything else, no server needed
pytest                   # both; live skips itself when unconfigured
```

It costs nothing to run — the fixture apps use template nodes rather than model
nodes — creates everything it needs, and deletes it afterwards, including what a
crashed earlier run left behind. A Dify that lacks a feature skips those tests
and says which capability was missing, because an absent feature is not a
failing SDK. See [tests/live/README.md](tests/live/README.md).


## API keys and credentials

### Where the key comes from

The API key is resolved in this order, first match wins:

1. `api_key=` passed to the client
2. the `DIFY_API_KEY` environment variable

```python
from dify_client import DifyApp

client = DifyApp()                       # reads DIFY_API_KEY
client = DifyApp("app-…")                # explicit
client = DifyApp(api_key=lambda: vault.read("dify"))
```

### The variables

These are the same names the [`difyctl`](https://github.com/langgenius/dify)
CLI reads, so a machine set up for one is set up for the other:

| Variable | What it is | Used by |
|---|---|---|
| `DIFY_HOST` | the Dify host, e.g. `http://localhost` | everything |
| `DIFY_API_KEY` | an app's Service-API key (`app-…`) | `DifyApp` |
| `DIFY_CONSOLE_TOKEN` | a console session — account-level | `DifyManagement` |
| `DIFY_CONSOLE_CSRF_TOKEN` | the second half of that session, needed to write | `DifyManagement` on Dify ≥ 1.17 |

`DIFY_HOST` is the one [`difyctl`](https://github.com/langgenius/dify) reads
too, so that much is shared. Its `DIFY_TOKEN` is **not** interchangeable: that
holds a `dfoa_` OAuth bearer for Dify's `/openapi/v1` surface, which is a
different API from the console one this client speaks. Handing a `dfoa_` token
to `DifyManagement` is refused with an explanation rather than a bare 401.

Since 1.17 the console API pairs the access token with a CSRF token and
rejects a write that arrives without one, so `DIFY_CONSOLE_TOKEN` alone can
read but not deploy. `DifyManagement.login(email, password)` collects both and
is the simpler path; to configure them by hand, take the `access_token` and
`csrf_token` cookies from a logged-in browser. A 401 on a write says which of
the two is missing rather than just "rejected".

A self-hosted Dify needs only `DIFY_HOST`: the Service API base is derived from
it as `<host>/v1`.

```bash
export DIFY_HOST=http://localhost:8088
export DIFY_API_KEY=app-…
```

`DIFY_API_BASE_URL` overrides that derivation for the unusual case where the
Service API does not sit at `<host>/v1`. Resolution order is the argument,
then `DIFY_API_BASE_URL`, then `<DIFY_HOST>/v1`, then Dify Cloud.

Passing a **callable** hands the SDK a way to fetch the key instead of the key
itself. It is resolved per request, so a rotating or short-lived key works
without rebuilding the client, and no long-lived secret sits on the instance.

Passing `api_key=""` explicitly is an error rather than a fallback to the
environment: a blank in code is a mistake, and silently using whatever the
shell holds hides it.

### Keys are not rendered

The key is held in a `SecretKey`, so the places that routinely capture objects
— `repr()`, `vars()`, tracebacks captured with locals, error reporters — see a
masked form:

```python
>>> repr(DifyApp("app-SECRETKEY123456"))
"DifyApp(base_url='https://api.dify.ai/v1', api_key=SecretKey('app-****3456'))"
```

`client.api_key` still returns the key itself, for the one caller that needs
it. Requests are unaffected — the `Authorization` header carries the real
value, and the SDK does not log headers.

### Testing workflows needs no provider key

Running a workflow locally would otherwise require the credentials of whatever
model it calls. `StubLLM` is the default route precisely so a CI job running
your workflow tests holds no credential worth stealing:

```python
result = wf.run(inputs, llm=StubLLM("a canned reply"))     # no key, no cost
```

Provider credentials are never read from the environment by `run()`. That is
deliberate: an ambient default would quietly turn a test run into a billed API
call. To exercise the workflow against a real model, run it on Dify — see
[Billed tests](#billed-tests).

### Exported DSL is safe to commit

`wf.to_yaml()` blanks secret environment variables, matching the default of
Dify's own exporter:

```python
token = wf.env_var("API_TOKEN", "t0p-s3cret", secret=True)
wf.to_yaml("app.yml")          # value: ''   — safe to commit
wf.run(inputs)                 # sees t0p-s3cret — the run stays in memory
```

Pass `include_secret=True` only for output going somewhere as guarded as the
secrets themselves. Model credentials are never serialised into the document at
all; they are an argument to the run.


## Billed tests

A stub tells you the graph is right. It cannot tell you whether the workflow
works *in Dify* — with the real model, the real plugins, and the version of the
server you actually run. That answer costs money, so the path to it is gated
rather than merely available.

The workflow is defined in code, so the app it runs as does not exist yet.
`apps.temporary()` creates it, publishes it, mints its key, and deletes it
afterwards — nothing to set up by hand:

```python
from dify_client import DifyManagement
from dify_client.workflow import requires_live

@requires_live
def test_the_real_model_obeys_the_prompt():
    with DifyManagement().apps.temporary(summarizer()) as managed:
        result = managed.client().workflows.runs.create(INPUTS)

    assert result.succeeded
    assert result.node("prompt")["output"].startswith("Summarise")
    assert len(result["answer"]) < len(draft)
```

The only credential this needs is a console token. The app, its published
version and its Service-API key are all created from the workflow and thrown
away with it, so the test is about *this code* rather than about whatever an
app in Dify has drifted into after someone edited it in the UI.

It is deleted even if the block raises. A hard crash can still leave one
behind.

### Deploying without the context manager

```python
management = DifyManagement()
result = management.apps.deploy(wf)                       # import + publish + key
result = management.apps.deploy(wf, app_id=ID, api_key=KEY)   # overwrite one you keep
result.raise_for_stage()

app = management.apps.open(result.app_id, api_key=result.api_key).client()
run = app.workflows.runs.create(INPUTS)
management.apps.delete(result.app_id)
```

`deploy()` does three things because a code-defined workflow needs all three,
and reports which it reached. Importing a DSL writes only the **draft**, and the
Service API runs the published version — a freshly imported app answers every
run with *workflow not published*. Keys are **reveal-once** and capped per app,
so pass `api_key=` when reusing an app rather than minting a new one each time.

`result.created` records whether this call created the app, so cleanup never
deletes one that was already there.

### Chatflows need a message

An app with an `answer` node is a **chatflow**, and the Service API serves it
at `/chat-messages` rather than `/workflows/run` — it rejects the wrong route
outright. `run_live()` follows `wf.mode`, but a chatflow is driven by a message,
so pass one:

```python
from dify_client.workflow import system

reply = wf.template("You said {{ q }}", variables={"q": system.query}, id="reply")
...
result = app.chat.messages.create("hello there", inputs={"style": "formal"})
```

`system.query` is where that message lands inside the workflow. `system` covers
Dify's `sys.*` variables generally — `system.user_id`, `system.conversation_id`,
`system.files` — and any name works, since the set differs by app mode and Dify
version.

An app that ends in an `end` node is a plain `workflow` and runs on `inputs`
alone. A local `run()` is unaffected either way: there the message is just
another start input.

A chatflow needs the **query and its start inputs**, not one or the other — the
query fills `sys.query`, and the start node's own variables are still required:

```python
app.chat.messages.create(text, inputs={"message": text})   # both
app.chat.messages.create(text)                # fails: "message is required in input form"
app.chat.messages.create("", inputs={"message": text})   # fails: query_required_for_chat
```

### Running an app that already exists

```python
result = wf.run_live(
    INPUTS,
    console=DifyManagement(), app_id=APP_ID,   # deploy over it first
    max_tokens=2000,
)
```

This needs `DIFY_API_KEY` for the run, and a console token only if you pass
`console=` to deploy. Reuse an app when you want its history and logs in Dify;
use `apps.temporary()` when you want the test to leave nothing behind.

### The gate

`run_live()` proceeds only when `DIFY_LIVE_TESTS` is set **and** there is a
credential to reach Dify with, so neither a stray variable nor a stray call can
start spending on its own. The gate is on the *testing* surface only: `DifyApp`
is the ordinary API client and is never gated — a normal call should not depend
on a test variable being set.

| Variable | Meaning |
|---|---|
| `DIFY_LIVE_TESTS` | this environment may make billed calls |
| `DIFY_CONSOLE_TOKEN` | create the app the workflow describes, key and all |
| `DIFY_API_KEY` | run an app that already exists |

Either credential opens the second gate; which one you need depends on the
path. `DIFY_LIVE_TESTS` is a flag, not a credential:

| Value | Effect |
|---|---|
| `1`, `true`, `yes`, `on` (any case) | live runs allowed |
| anything else, or unset | live runs refused |

```bash
DIFY_LIVE_TESTS=1 pytest -m billed          # one command only
```

Prefer setting it per command rather than exporting it into your shell. An
exported flag stays on for every later run in that terminal, which is exactly
how an afternoon of test runs turns into an invoice.

A console token is a full-privilege account credential — see
[Deploying to Dify](#deploying-to-dify) before putting one in CI. In CI, scope
the job so it runs where you mean it to:

```yaml
- name: Billed workflow tests
  if: github.event_name == 'workflow_dispatch'
  env:
    DIFY_LIVE_TESTS: "1"
    DIFY_CONSOLE_TOKEN: ${{ secrets.DIFY_CONSOLE_TOKEN }}
  run: pytest -m billed
```

Nothing in the SDK can stop a workflow that is configured to spend on every
commit.

### Skipping and deselecting

`@requires_live` skips the test when either variable is missing, with a reason
that names the one that is:

```
SKIPPED - DIFY_LIVE_TESTS is not set; live runs call real models and cost money
```

It also applies the `billed` marker, so a CI job can drop these outright with
`pytest -m "not billed"` rather than trusting its environment to be clean.

### What a run cost

Every result carries `usage`, whether it came from a stub or from Dify:

```python
>>> print(result.usage)
1,240 tokens (0 in / 0 out) · 0.000214 USD
```

A stubbed run reports nothing, which is what makes *this test spent nothing* an
assertion rather than a belief:

```python
assert not wf.run(inputs, llm=StubLLM("ok")).usage
```

`run_live()` streams rather than using blocking mode, because Dify's blocking
response reports only a workflow-wide token total — per-node inputs, outputs
and **price** arrive as node events. That is also why a `RunResult` from Dify
has the same shape as one from a local run, and assertions carry across
unchanged.

Two budget limits, both checked after the run:

- `max_tokens` works everywhere tokens are counted.
- `max_cost` needs the run to have reported a price. If it did not, that is an
  error rather than a pass — a priced check that cannot run is false assurance.
  A run that consumed nothing at all satisfies either limit.

Be clear about what these do: the call was already made and the charge already
incurred. What they stop is the same overspend passing unnoticed on every run
after this one — a prompt that quietly grew, or an iteration node that stopped
terminating.

### Running against a real model without Dify

`run(credentials=...)` executes the workflow locally against real providers
through graphon's own plugin adapter. It is not the billed path documented
here and it is not what Dify runs — Dify injects its own model runtime, so a
pass there says less than a pass on a real instance. It also needs the
`dify-plugin-daemon-slim` binary and downloads the provider plugin locally. Use
it only if you specifically want local execution.

### What a live run needs from the workflow

Dify installs a workflow's declared plugins when it imports the DSL, so an
undeclared provider deploys into an app that cannot run. Declare it:

```python
wf.depends_on("langgenius/openai:0.3.8@592c8252795b…")
```

`wf.missing_plugin_dependencies()` lists what is missing, and `run_live()`
checks it before deploying, so the failure names the fix.

## Agents as code

An Agent's configuration — its *soul* — is defined by pydantic models that live
inside the Dify server and are **not published as a package**. Workflow node
schemas come from [`graphon`](https://github.com/langgenius/graphon), which is
why this SDK can build and validate a workflow against the same definitions the
server uses. Nothing equivalent exists for Agents yet.

Rather than copy a thousand lines of schema into this SDK and watch it drift,
`Agent` carries the soul as data it does not interpret. What it does enforce is
the part that needs no schema: the envelope Dify expects, and the removal of
credentials before anything is written to a file.

### Building one from scratch

`Agent.create()` starts from the soul Dify itself writes for a new Agent, so a
hand-made one has the shape the importer expects:

```python
from dify_client import Agent, DifyManagement, dify_tool

catalog = console.tools()
agent = Agent.create(
    "support-triage",
    instruction="Decide whether an incoming message needs paging.",
    model="langgenius/openai/openai:gpt-4o-mini",
    tools=[dify_tool(catalog["time"]["current_time"])],
    role="On-call triage",
    temperature=0.2,
)
console.apps.deploy(agent)
```

`agent.instruction`, `agent.model`, `agent.use_model()` and `agent.add_tool()`
cover the common fields; `agent.soul` stays a dict for the rest. Agents live on
their own roster — `console.agents()` — and appear in neither the generic app
list nor `OpenApiClient.apps()`.

### Giving an Agent skills

A Dify skill is a zip holding a `SKILL.md` whose frontmatter names it and says
when to use it — the same shape a Claude Code skill has:

```python
from dify_client import Skill

skill = Skill.from_directory("skills/paging-policy")
console.skills.create(skill)                # uploads, then publishes
agent.use_skill("paging-policy")           # bound by name, travels in the DSL
```

`name` must be lower-case words joined by hyphens; `description` is what tells
the Agent when to reach for the skill. Both are checked before anything is
uploaded. `import_skill` publishes by default, because an Agent binds to a
published version and an import alone leaves a draft.

### Editing an existing one

When the Agent was configured in the UI, edit the export rather than rebuilding
it: the export is known-good.

```python
from dify_client import Agent, DifyManagement

console = DifyManagement()
agent = Agent.from_yaml(console.apps.export(APP_ID))

agent.soul["model"]["completion_params"]["temperature"] = 0.1
agent.role = "On-call triage"

agent.to_yaml("support-triage.yml")     # commit this; diff the next change
console.apps.deploy(agent)              # and deploy it back
```

Configure an Agent in the Dify UI once, export it, and from then on the file in
your repository is the source of truth.

### What this does and does not give you

**Does**: a committable, diffable file; credentials stripped on the way out;
deployment and key minting through the same `DifyManagement` as
workflows; a faithful round trip.

**Does not**: type checking, completion, or validation of the soul. `agent.soul`
is a plain dict. A typo in a field name is caught by Dify on import, not here.

That gap closes when Dify publishes the Agent config models the way it
published graphon. Until then, editing an export beats authoring from scratch,
because the export is known-good.

### Secrets

`to_yaml()` blanks anything credential-shaped — keys containing `credential`,
`secret` or `password`, plus `api_key`, `token`, `access_token`,
`refresh_token` and `*file_id` — matching the rule Dify's own portable-package
builder applies. The rule is by key name rather than by schema, which is what
lets it work on a soul this SDK does not understand.

```python
agent.to_yaml()                      # safe to commit
agent.to_yaml(include_secret=True)   # only for somewhere as guarded as the secrets
```

### Deploying replaces, it does not update

Dify's importer **only creates new Agent apps**; it will not overwrite one.
Passing `app_id` is rejected here rather than failing halfway:

```
Dify's importer only creates new Agent apps; it cannot overwrite one.
Deploy without app_id and keep the new id, or edit the agent in the console.
```

Workflows do not have this restriction — `console.apps.deploy(wf, app_id=...)`
overwrites, which is what keeps code the source of truth for them.


## The two APIs Dify offers

Deploying needs an API that can create apps, and Dify has two. This SDK speaks
both, and which one you can use depends on the deployment.

| | `DifyManagement` | `OpenApiClient` |
|---|---|---|
| Path | `/console/api` | `/openapi/v1` |
| Intended for | the Dify web console | programmatic clients, including `difyctl` |
| Credential | a console session (`DIFY_CONSOLE_TOKEN`) | a scoped OAuth bearer (`DIFY_TOKEN`) |
| Available | always | only when the operator turns it on |
| Publish a workflow | yes | **no** |

`OpenApiClient` is the better surface where it exists: the credential is scoped
and expiring rather than a full console session, and one `:run` path serves
every app mode, so a chatflow and a workflow are called identically.

It is off by default, twice over — `OPENAPI_ENABLED` must be true, and the
caller's `client_id` must appear in `OPENAPI_KNOWN_CLIENT_IDS`, which ships
holding only `difyctl`. So `DifyManagement` remains the path that works
anywhere.

```python
from dify_client import DifyManagement, OpenApiClient

if OpenApiClient.available():
    api = DifyManagement.login(email, password).open_api(client_id="difyctl")
    app = api.deploy(wf)
    result = api.run(app.app_id, {"name": "Dify"})
else:
    console = DifyManagement.login(email, password)
    ...
```

### Getting a bearer without a browser

Dify's device flow normally sends a person to a browser to approve the request.
With a console session already in hand, this SDK approves it directly:

```python
token = DifyManagement.login(email, password).mint_openapi_token(client_id="difyctl")
# dfoa_… — set it as DIFY_TOKEN, the same variable difyctl reads
```

An unregistered `client_id` is refused with the setting the operator has to
change, rather than a bare `unsupported_client`.

### File inputs

A file input is not sent inline — it is uploaded first and referenced by id:

```python
uploaded = api.upload_file(app_id, "report.pdf")
api.run(app_id, {"doc": {
    "transfer_method": "local_file",
    "upload_file_id": uploaded["id"],
    "type": "document",
}})
```

A path, or any open binary file, is accepted. Dify types the upload by its
extension and answers 415 for one it cannot type, so an unrecognised name is
refused here instead — pass `content_type=` to settle it. A `BytesIO` has no
name of its own, so it needs `filename=`.

For the Service API the equivalent is `DifyClient.file_upload(user=…, files=…)`,
which is per-tenant rather than per-app.


### Publishing still goes through the console

`/openapi/v1` can import a DSL but not publish it, and the Service API runs only
published work. So a full cycle mixes the two:

```python
result = api.deploy(wf)                        # /openapi/v1
console.apps.publish(result.app_id)        # /console/api — no equivalent
run = api.run(result.app_id, inputs)           # /openapi/v1
```


## Code nodes without Dify's sandbox

A code node posts its program to `dify-sandbox`, so on its own it fails in a
local test. Two ways round it, neither needing Docker:

```python
from dify_client.workflow import StubCode, LocalSandbox

wf.run(inputs, code=StubCode({"score": 0.9}))   # do not run it at all
wf.run(inputs, code=LocalSandbox())             # run it here, confined by the OS
```

| | Runs your code | Confinement | Needs |
|---|---|---|---|
| `StubCode` | no | — | nothing |
| `LocalSandbox` | yes | the host's own | nothing on macOS; `bubblewrap` on Linux |

### What a code node may import

This is the difference that bites. Dify's sandbox sees its own interpreter and
whatever packages that deployment installed — **not** your project's
virtualenv. A code node allowed to reach your virtualenv would `import pandas`
happily in a test and fail in production, so `LocalSandbox` hides it:

```python
LocalSandbox()                        # standard library only
LocalSandbox(packages=["httpx"])      # plus httpx and its dependencies
LocalSandbox(packages=["httpx"]).importable()
# ['anyio', 'certifi', 'h11', 'httpcore', 'httpx', 'idna', 'sniffio']
```

Erring strict is deliberate: a package missing locally is a visible failure,
while a package present only locally is a workflow that ships broken.

To find out what your Dify's sandbox has, deploy a code node that tries the
imports you care about and read its output — the stock image ships `httpx`, but
an operator can add to it.

### What LocalSandbox does and does not match

The program it runs is not a re-implementation: graphon's template transformer
builds it, so the source executed locally is byte-identical to what Dify would
have sent to its sandbox.

The **confinement** differs. Dify isolates with seccomp, a Linux kernel
facility that does not exist on macOS, so `LocalSandbox` uses what the host
offers — Seatbelt (`sandbox-exec`) on macOS, bubblewrap on Linux. Your logic
behaves the same; a program that trips one confinement's limits will not
necessarily trip the other's.

The **interpreter version** differs too: `LocalSandbox` uses the Python running
your tests, and Dify's sandbox uses its own. Pass `python="/path/to/python3.x"`
to match a specific one.

If the exact confinement is what you need, a `dify-sandbox` you already run
answers over HTTP — a Dify stack on this machine has one:

```python
wf.run(inputs, credentials={"code": {
    "execution_endpoint": "http://localhost:8194",
    "execution_api_key": "dify-sandbox",
}})
```

Network access is **off by default**, which is stricter than Dify's shipped
config (`enable_network: True`). A node that makes requests therefore fails
here until you say so, rather than a test run quietly reaching the internet:

```
The code node tried to use the network, which LocalSandbox blocks by default.
Dify's own sandbox allows it, so pass LocalSandbox(network=True) if this node
is meant to make requests.
```

`LocalSandbox(confine=False)` skips isolation entirely. That runs the
workflow's code with the full privileges of the test process, so keep it for
code you wrote and a host you are willing to hand to it.

## Workflows as code

Install the extra:

```
pip install "dify-client[workflow]"
```

Workflows are built against [`graphon`](https://github.com/langgenius/graphon),
the same engine Dify runs in production, so the node schemas here are the ones
the server itself validates against rather than a copy that drifts.

### Define a workflow

```python
from dify_client.workflow import Workflow, paragraph, select

wf = Workflow("tone-rewriter")
start = wf.start([
    paragraph("draft", label="Draft text"),
    select("tone", ["formal", "casual"], label="Tone"),
])
prompt = wf.template(
    "Rewrite the following in a {{ tone }} tone.\n\n{{ draft }}",
    variables={"draft": start["draft"], "tone": start["tone"]},
    id="prompt",
)
rewrite = wf.llm(prompt.output, model="langgenius/openai/openai:gpt-4o-mini", id="rewrite")
answer = wf.answer(rewrite.output)
wf.connect(start, prompt, rewrite, answer)
```

Indexing a node gives a reference to one of its outputs, which renders as the
`{{#node.field#}}` template or the `["node", "field"]` selector, whichever the
surrounding configuration needs. `node.output` is shorthand for a node's
conventional single output, so `rewrite.output` is `rewrite["text"]`.

An `answer` node makes the app a chatflow (`advanced-chat`); use `wf.end(...)`
instead for a plain `workflow` app.

### Test it without a Dify server

`StubLLM` stands in for the model, so the rest of the graph — prompt assembly,
branching, variable flow — can be asserted on in CI, offline and for free.

```python
from dify_client.workflow import StubLLM

def test_the_tone_reaches_the_prompt():
    stub = StubLLM("We will be shipping tomorrow.")
    result = wf.run({"draft": "we ship it tomorrow", "tone": "formal"}, llm=stub)

    assert result.succeeded
    assert "formal tone" in str(stub.calls[0][0].content)
    assert result.node("prompt").succeeded
    assert result["answer"] == "We will be shipping tomorrow."
```

`StubLLM` also takes a callable, which receives the prompt messages the
workflow actually built and returns the reply:

```python
StubLLM(lambda messages: "SHORT" if "casual" in str(messages[0].content) else "LONG")
```

Without a stub, an LLM node cannot be built at all — graphon resolves provider
credentials while constructing the node — so `run()` raises `WorkflowRunError`
rather than returning a failed result. Pass real `credentials=...` to call a
real model.

`run()` returns a `RunResult`: `outputs` and `result["answer"]` for the
workflow-level result, `result.node(id)` for any individual node's `inputs`,
`outputs`, `status` and `error`.

### Export and import

```python
wf.to_yaml("tone-rewriter.yml")
```

This writes a `kind: app` DSL document, the same format the Dify console
imports and exports. The document that passes your tests is the document you
deploy — there is no second representation to keep in sync.

Declare the plugins a workflow needs so Dify installs them on import:

```python
wf.depends_on("langgenius/openai:0.3.8@592c8252795b5f75807de2d609a03196ed02596b409f7642b4a07548c7ff57ef")
```

### Nodes without a helper yet

Typed helpers exist for `start`, `template`, `llm`, `code`, `answer` and `end`.
Every other node type graphon supports goes through `wf.add()` with its entity:

```python
from graphon.nodes.if_else.entities import IfElseNodeData

branch = wf.add(IfElseNodeData(title="Branch", cases=[...]), id="branch")
wf.connect(branch, urgent, handle="true")
```

See [`examples/`](examples/) for five runnable scripts — the first three need
nothing installed beyond this SDK.

## Claude Code skill

`.claude/skills/dify-workflow/` teaches an agent this SDK — the builder API, the
gotchas that are only learned by hitting them, and the deploy path. It loads
when someone asks to build, test or deploy a Dify workflow in this repository.

To use it from anywhere rather than only here:

```bash
ln -s "$PWD/.claude/skills/dify-workflow" ~/.claude/skills/dify-workflow
```


## Triggers: workflows that start themselves

A workflow normally waits to be called. A trigger node replaces the start node
and has Dify start the run instead — on a clock, or when a webhook is posted to:

```python
from dify_client.workflow import Workflow, body_field, header

wf = Workflow("order-hook")
hook = wf.webhook(
    method="post",
    headers=[header("x-signature", required=True)],
    body=[body_field("order_id", required=True), body_field("total", "number")],
)
wf.connect(hook, wf.end({"order_id": hook["order_id"], "total": hook["total"]}))
```

Each declared field becomes an output of the node, so `hook["order_id"]` reads
the posted value. Headers may only be strings; query parameters add numbers and
booleans; body fields add objects, arrays and files. Asking for a type the
surface does not allow is refused here rather than at import.

A schedule takes either a cron expression or a frequency:

```python
wf.schedule("0 2 * * *", timezone="Asia/Tokyo")
wf.schedule(frequency="weekly", at={"time": "9:00 AM", "weekdays": ["mon"]})
```

Both are the same trigger to Dify. The difference is the editor: it renders the
second as a form and shows the first only as text.

### A trigger is not real until you publish

Importing a DSL writes a draft, and a trigger node in a draft is just a drawing.
Dify materializes the trigger — and mints the webhook's URL — on publish:

```python
app = console.apps.deploy(wf)
console.apps.publish(app.app_id)          # without this there is no trigger

console.apps.triggers.list(app.app_id)
# [Trigger(id='01a0…', type='trigger-webhook', title='Webhook',
#          node_id='trigger_webhook', status='enabled')]

console.apps.triggers.webhook(app.app_id, "trigger_webhook").url
# 'http://localhost/triggers/webhook/jdKQ2hKTUePj_4ZXMRazjw-q'
```

`enable_trigger(app_id, trigger_id, enabled=False)` pauses one without
unpublishing the app; `enabled=True` puts it back.

### Triggers cannot be run locally

The trigger node types live in Dify's own code, not in graphon, so `wf.run()`
has no implementation to call and a workflow built around one only runs on a
real Dify. Test the rest of the graph behind a start node, then swap the trigger
in for deployment — or run the deployed app with `wf.run_live()`.


