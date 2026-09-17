# Changelog

## 1.0.0 — unreleased

The first release since the SDK was checked against a running Dify. Everything
below was verified against Dify 1.17.1, not inferred from the docs.

**Why 1.0.0 and not 0.2.0.** The published API is replaced rather than
extended: `DifyClient`, `ChatClient`, `WorkflowClient`, `KnowledgeBaseClient`
and both async clients are gone, and nothing imports from `dify_client.models`
any more. A minor bump on the 0.x line would have let `pip install -U
dify-client` break working code with nothing in the number to say so. See
*Migrating*.

**This release replaces the client API.** The package was built against an old
Dify and had grown a shape that no longer fit it: a class per app mode, every
method returning a raw `httpx.Response`, and a thousand lines of classes calling
endpoints Dify has never served. Rather than layer over that, it is rebuilt.

The vocabulary is Dify's own — an app, a run, a node execution, a message, a
conversation, a document — and the API is arranged around those nouns.
**Migrating** below maps the old names to the new.

### The new shape

A client holds the connection and the credential. Operations hang off what they
act on, and return the thing rather than an HTTP response:

```python
from dify_client import DifyApp

app = DifyApp(api_key="app-…", user="alice")

message = app.chat.messages.create("Hello")       # -> Message
run = app.workflows.runs.create({"text": "…"})    # -> WorkflowRun
```

Three entry points, because Dify scopes three credentials and pretending
otherwise helps nobody: `DifyApp` (an app's key), `DifyKnowledge` (a dataset
key), `DifyManagement` (a console session).

**Nouns that were previously conflated.** `NodeExecution` is one run of a node,
not the node — a node inside a loop has one execution per pass, which is what
made usage undercount. `run_id`, `task_id` and `node_id` stay distinct rather
than flattening to `id`, because they address different things.

**A stream is how a run is observed**, not a different kind of call. It yields
typed events and then answers what the run came to:

```python
with app.workflows.runs.stream(inputs) as stream:
    for event in stream:
        if event.type == "node_finished":
            print(event.execution.node_id, event.execution.usage)
    run = stream.get_final_run()
```

Four things Dify keeps apart and this now does too: closing the stream is not
stopping the run; a pause is not a failure; a dropped connection says nothing
about the run; and an HTTP 200 is not a successful run — Dify reports a
mid-stream failure as an event after the status line has gone.

**The lifecycle is in the API.** Importing writes a draft; the Service API runs
the published version. `deploy()` reports the stage it reached rather than
raising, because "nothing happened" and "the app exists but is unpublished" call
for different responses:

```python
result = management.apps.deploy(workflow)
result.stage        # not-imported / drafted / published / runnable
result.imported     # a draft exists, even though publishing failed
result.created      # this call created it — what makes deleting it safe
```

**States that are independent are kept independent.** A deploy reports
`imported`, `published`, `has_key` and `indeterminate` as separate facts rather
than a position on a ladder — folding them let a later rung stand in for an
earlier one, so a failed import over an existing app reported `runnable` and
published whatever draft was already there. Likewise a message is `finished`
only when Dify says so, so a stream cut short does not pass for a complete
answer, and a run's `usage` (what Dify charged) is separate from its
`node_usage` (what this client watched) — reopening a finished run reports the
total and no node events, which used to read as free.

**Typed where Dify is fixed, open where it is not.** Run ids, statuses, usage
and events are typed; a workflow's `inputs` and `outputs` stay dictionaries
because they differ per app; and every event keeps its whole payload, so a field
a newer Dify adds still reaches the caller.

### Fixed

Bugs that produced a wrong answer rather than an error, which is why a green
test suite did not catch them:

- **Streaming did not stream.** `stream=True` was accepted, documented, and
  never passed to httpx, so `response_mode="streaming"` waited for the whole
  generation and then replayed it from a buffer. Every mode that takes
  `response_mode` now streams, `WorkflowClient.run` included.
- **The async client never raised.** A 401 came back as a response object while
  the sync client raised `AuthenticationError`. It also never retried, where the
  sync client retried three times. Both now behave identically.
- **Retries could bill the same run several times.** A `ReadTimeout` means the
  request *was* sent; retrying `POST /workflows/run` three more times could
  start four billed runs. Failures after the request was sent are now retried
  only for idempotent methods.
- **Usage was undercounted inside loops.** Node results were keyed by node id,
  so a node that ran three times reported once — and a `max_tokens` budget
  passed runs that had already gone over. `result.usage` now sums every
  execution; `result.runs_of(node_id)` exposes them.
- **`run_live()` tested the wrong version.** It imported the DSL but never
  published it, and the Service API runs the published version — so a live run
  measured whatever was published before.
- **A failed `provision()` left the app behind.** `ephemeral_app()` never
  reached its cleanup when publishing or key creation failed.
- **`None` query parameters went out as empty strings.** httpx renders
  `{"limit": None}` as `?limit=`, which Dify's typed query models reject, so a
  plain `get_conversations(user)` answered 422.
- **Invented size limits.** Every request body was rejected client-side for a
  string over 10,000 characters, a list over 1,000 items or a dict over 100
  keys. A knowledge-base document longer than a few pages could not be posted.
  Dify imposes none of it.
- **Parameters that did not match Dify.** `get_conversations` sent `pinned`,
  which Dify ignores — the filtered list was the whole list. See *Migrating*.
- **`iter_lines(decode_unicode=True)`** in the README is a `requests` call;
  httpx raises `TypeError` for it.
- **`app.models()` could never have worked.**
  `/workspaces/current/models/model-types/…` is the one Service-API route Dify
  guards with the *dataset* token, so an app key answered "Access token is
  invalid". It is `knowledge.models()` now.
- **Async listings could not be continued.** They returned a page with no way
  to fetch the next one, so `all()` existed on the sync path and not the async
  one. They return `AsyncPage` now.
- **The release pipeline could not build the package.** `uv run build` looks
  for a command called `build`; the package installs `pyproject-build`. Every
  release tag failed at that step. It builds with `uv build` now, runs the
  tests first — a tag does not trigger the CI workflow — and installs the
  built wheel both with and without the `workflow` extra.
- **The sdist swept up the working tree.** Anything lying beside the package
  when it was built was published with it, including a file written by running
  one of the examples. The contents are named now.
- **Two dependencies nobody used.** `aiofiles` was installed on every machine
  and imported nowhere, and `httpx[http2]` pulled in `h2` for HTTP/2 that this
  SDK never asks for. Both gone; a test now fails if a declared dependency is
  not imported.
- **`timeout=` next to `http_client=` is now refused.** It was accepted,
  stored and then ignored — a request made through a caller's own client uses
  that client's timeout — so anyone who passed one did not get it and was told
  nothing. The message says where to put it instead.
- **`enable_logging=True` no longer caps its own logger at INFO**, which had
  quietly deleted every debug line (bodies, parameters, uploads) for an
  application that attached a DEBUG handler to go looking for them.
- **A price reported without a currency is kept** rather than discarded as
  "nobody said". The amount is real; only the unit is unknown, and
  `usage.currency` is `""` to say so.
- **`page.all()` could ask forever.** A cursor listing that came back with the
  same cursor — Dify does this when the cursor's message has been deleted, and
  any caching proxy can — fetched the same page for as long as the process
  lived. A page will not continue from a cursor it has already used, and every
  walk now stops at 10,000 items with `PageLimitReached` (which carries what it
  read) rather than trusting `has_more` indefinitely.
- **A run total of zero erased the per-node prices under it.** `merged_with`
  preferred the aggregate whenever one was reported, so a model Dify has no
  pricing for could report a billed run as free. An actual amount now wins
  over a zero, whichever side reported it.
- **A file upload's bytes went into the debug log.** Names and sizes now.
- **Message history repeated itself.** Dify sends a page oldest-first and
  continues from the *first* id on it, because the next page is older; the SDK
  continued from the last, asking for everything older than the newest message
  on the page — most of the page again, every time.
- **A moderated answer came back as the rejected one.** Output moderation makes
  Dify emit `message_replace` with the whole replacement, and that replacement
  is what it saves. The SDK ignored the event and returned the original text.
- **A paused run read back said it was not paused.** `retrieve()` reports
  `status: "paused"` and no form tokens — those were raised on a stream this
  client may never have watched — and `paused` was read off the tokens alone. A
  blocking run that pauses now also carries the tokens to resume it, from
  `data.reasons`, and a paused chatflow reply is no longer reported as finished.
- **Deploying an Agent never produced a usable app.** Dify holds an Agent's
  Soul as a draft until it is published on the roster, and answers every key
  request before that with "Publish the Agent before enabling Web App or API
  access" — so `deploy(agent)` reported published-with-no-key. It now publishes
  at `/agent/<id>/publish`. The same Agent passed as DSL text was worse: a
  string has no `.mode`, so it was sent to `/apps/<id>/workflows/publish`,
  which Dify serves for `workflow` and `advanced-chat` only. The app mode Dify
  reports on import is what decides now.
- **Answering one of two human-input forms cleared both.** Dify's
  `human_input_form_filled` names the node and carries no token, so the SDK's
  token-matching never matched and fell back to clearing everything — leaving
  the second form unreachable and the run reported as no longer paused. Forms
  are tracked by node now, and `run.paused_nodes` says where a run is waiting.
- **A pause Dify gave no token for was invisible.** `human_input_required`
  carries a null token for a form meant to be answered in Dify's own UI, and
  `workflow_paused` was ignored entirely, so such a run reported
  `status="unknown"` and `paused=False`.
- **`AsyncFiles.preview_url` did not exist.** The parity check listed the
  resources it compared by hand, and this one was not on the list; it now
  discovers them.
- **`Pipeline.run` returned `Any`** — a dict for one mode, a raw
  `httpx.Response` for another. Dify runs two different things behind that one
  route, and the SDK left the caller to work out which had happened.

### Added

**A resource-shaped API.** The client holds the connection and the credential;
what you can do hangs off what it acts on, and return values are the things
themselves rather than HTTP responses:

```python
app = DifyApp(api_key="app-…", user="alice")
message = app.chat.messages.create("Hello")     # -> Message
run = app.workflows.runs.create({"text": "…"})  # -> WorkflowRun
```

- `DifyApp` / `AsyncDifyApp`, with `app.chat.messages`,
  `app.chat.conversations`, `app.workflows.runs` and `app.files`.
- `DifyKnowledge` and `DifyManagement` — the same three credentials Dify
  actually has, named as entry points instead of left implicit.
- `WorkflowRun`, `Message`, `NodeExecution`, `Conversation`, `UploadedFile`,
  `AppInfo`, `Usage` — typed, and carrying the ids the next call needs.
  `run_id`, `task_id`, `node_id` are kept distinct rather than flattened to
  `id`, because they address different things.
- `WorkflowRunStream` / `MessageStream`: a stream is how a run is *observed*.
  It yields typed `RunEvent`s and then answers `get_final_run()` /
  `get_final_message()`. Closing it stops watching, not the run; a pause is not
  a failure; a dropped connection says nothing about the run. Every event keeps
  its whole payload, so a field a newer Dify adds still reaches the caller.
- `run.paused`, `run.failed`, `run.finished` — three separate questions, where
  before only `succeeded` existed.
- `run.runs_of(node_id)` and `run.executions`: a node and one execution of it
  are different things, which is what made a looped node report only its last
  pass. Each execution carries its own `execution_id`, so a redelivered event
  on reconnect is not billed twice.
- `HistoryMessage` and `Page[T]`: a recorded turn is not a generated reply. It
  carries the `query` that prompted the answer, the files, the rating and the
  cost, none of which a fresh reply has. Reusing one type dropped the query,
  leaving a transcript of answers to questions nobody could see.
- `Transport` and `Console` protocols: resources declare which kind of client
  they plug into, so wiring one to the wrong credential is a type error rather
  than a runtime 404.

- `client_for(api_key)` — asks the app its mode and returns the client that
  serves it.
- `ConsoleClient.apps()`, `.app()`, `.open_app()` — list the workspace's apps,
  find one, and take hold of one that already exists. Previously only
  `provision()` existed, which always created.
- `stream_text()` and `stream_events()` (plus `astream_*`) — read a streaming
  response without writing the SSE loop. They raise on the mid-stream `error`
  event Dify sends after a 200, which used to end the stream silently.
- Run tracking: `RunResult` now carries `run_id`, `task_id`, `conversation_id`,
  `message_id` and `pending_forms`, with `.paused`. `DeployedApp.stop()`,
  `.resume()`, `.submit_form()` and `.client()` use them.
- Human-input forms: `get_human_input_form()`, `submit_human_input_form()`,
  `get_workflow_events()`.
- Triggers: `wf.webhook()`, `wf.schedule()`, and `ConsoleClient.triggers()`,
  `.webhook_trigger()`, `.enable_trigger()`.
- `OpenApiClient.upload_file()`, `CompletionClient.stop()`,
  `DifyClient.get_app_feedbacks()`, `.get_end_user()`, and six
  `KnowledgeBaseClient` methods for child chunks and document downloads.
- `probe()` and `Compatibility` — ask a Dify what it supports rather than
  inferring it from a version string. Three answers, not two: available, absent,
  or not determined because the credential to ask was missing. `require()`
  refuses on the third, since not knowing is not permission to proceed.
- A live harness in `tests/live/`: contract tests against a running Dify,
  skipped without one, costing nothing to run, cleaning up after itself and
  after crashed earlier runs. It skips what a server cannot do rather than
  reporting an absence as a failure.
- Full Service-API coverage, and a test that keeps it that way: it reads the
  routes out of Dify's controllers and fails if one has no call reaching it.
  Added on the way: knowledge-base tags, the RAG pipeline, metadata renaming
  and Dify's built-in fields, reading one document or segment back, downloading
  a message's files, writing a conversation variable, and pinning a run to a
  published workflow version.
- `Page[T]` on every listing, and `AsyncPage[T]` on every async one: `page.all()`
  walks the pages Dify hands out however it pages them — by number, by cursor,
  or not at all. This is also what fixed "find an app by name" looking at only
  the first hundred.
- `AsyncDifyKnowledge`, and the `DifyApp` methods async had never had
  (`open`, `server_info`, `probe`, `parameters`, `meta`, `site`, `feedbacks`,
  `end_user`). A test now asserts, by reflection, that every sync verb has an
  async counterpart with the same arguments.
- `PipelineIngestion`, and `Pipeline.run_draft()` / `stream_draft()`. A
  published pipeline run enqueues documents and answers with the batch they
  share; a draft run executes the graph and reports a `WorkflowRun`. Two
  different things, now two different return types.
- `dify_client.__version__`, and a `User-Agent` that names the SDK, the Python
  and the httpx it is running on. Dify saw `python-httpx/0.28.1` before.
- `error.server_version`, `.server_env` and `.trace_id`, from the `X-Version`,
  `X-Env` and `X-Trace-Id` headers Dify puts on every response. The version is
  in the error message too, where a pasted traceback will show it.
- **429 is waited out** rather than raised on sight, honouring `Retry-After`
  (a delay or a date) for any method — a 429 means Dify refused the request, so
  repeating it cannot duplicate work. A server asking for longer than a minute
  still raises rather than blocking.
- `client.with_timeout(seconds)` — a timeout for one block, scoped to the
  calling thread or task, for the calls that are nothing like the rest.
- Typed answers where a caller has to read a field to make the next call:
  `AppParameters` / `InputField` (the input form arrives as a list of
  single-key objects, and `field.name` is the key a run takes — not
  `field.label`), `SiteSettings`, `ModelProvider` / `Model` (a model now
  carries the provider it belongs to, which Dify leaves out of its payload,
  and a localised label becomes one string), `Tag` (`binding_count` arrives as
  a string) and `MetadataField` (a built-in field has no id, which is what
  `field.built_in` reads). Open-ended shapes — `app.meta()`, run logs,
  datasource descriptors — stay dicts on purpose, and every type carries the
  whole answer on `.payload`.
- `py.typed`, so the annotations reach type checkers at all.
- `http_client=` on every Service-API client, matching `ConsoleClient`.
- `DIFY_CONSOLE_CSRF_TOKEN`. Dify 1.17 pairs the console token with a CSRF
  token and rejects writes without it, so an access token alone could read but
  not deploy.

### Removed

- Six async classes — `AsyncEnterpriseClient`, `AsyncSecurityClient`,
  `AsyncAnalyticsClient`, `AsyncIntegrationClient`, `AsyncAdvancedModelClient`,
  `AsyncAdvancedAppClient` — about 1,100 lines calling routes that do not exist
  in Dify's Service API. Checked against all 64 real routes.
- Seventeen methods that named console-API paths and sent them to `/v1`, so
  every one answered 404.
- Nine duplicate aliases (`*_with_response`, `*_with_pagination`, `*_by_type`)
  byte-identical to the plain method.
- `dify_client.models` — response dataclasses nothing constructed or returned.
- `examples/advanced_usage.py`, whose streaming example could not run.
- `dify_client.streaming`, with `StreamEvent` and the standalone `stream_text`
  / `stream_events` helpers. `RunEvent` is a strict superset of `StreamEvent`,
  and the helpers took a raw `httpx.Response` that no method hands out any
  more — two event types for the same bytes, one of them unreachable. SSE
  decoding lives in `dify_client.sse` now and is internal.

### Migrating

| Was | Now |
|---|---|
| `except APIError` | unchanged, but it now also catches `AuthenticationError`, `RateLimitError`, `ValidationError` and `FileUploadError` |
| `except TimeoutError` (ours) | `except RequestTimeout` — the old name is an alias, but it shadowed the builtin |
| `except DatasetError` / `exceptions.WorkflowError` | gone; nothing raised them. The builder's `WorkflowError` lives in `dify_client.workflow` |
| `get_conversations(pinned=…)` | drop it; Dify has no such filter. `sort_by=` is real |
| `get_conversation_variables(page=…)` | `last_id=` — this endpoint pages by cursor |
| `message_feedback(id, "like", user)` | unchanged; `rating=None` now revokes, and `content=` carries text |
| `text_to_audio(text, user)` | unchanged; `voice=` is often required, and `message_id=` speaks an existing message |
| `app.run(...)` | needs `DIFY_LIVE_TESTS`, or `billed=True` to say the spend is intended |
| the 17 removed methods | `ConsoleClient` equivalents — see *Methods that were removed* in the README |
| `ChatClient(key).create_chat_message(inputs, query, user)` | `DifyApp(key, user=…).chat.messages.create(query, inputs=…)` → `Message` |
| `WorkflowClient(key).run(inputs, user=…)` | `DifyApp(key, user=…).workflows.runs.create(inputs)` → `WorkflowRun` |
| `CompletionClient(key).create_completion_message(...)` | `app.completions.create(inputs)` |
| `client.get_conversations(user)` | `app.chat.conversations.list()` |
| `client.file_upload(user, files)` | `app.files.upload(path)` → `UploadedFile`, with `.reference()` |
| `client.message_feedback(id, rating, user)` | `app.chat.messages.feedback(message, rating)` |
| `client.text_to_audio(...)` / `audio_to_text(...)` | `app.audio.speak(...)` / `app.audio.transcribe(...)` |
| `KnowledgeBaseClient(key, dataset_id=…)` | `DifyKnowledge(key)`, then `knowledge.datasets` / `knowledge.documents(dataset)` — the dataset is named per call, not per client |
| `kb.hit_testing(query)` | `knowledge.datasets.search(dataset, query)` → `list[RetrievalHit]` |
| `ConsoleClient` | `DifyManagement`, with `management.apps`, `.apps.keys`, `.apps.triggers`, `.skills`, `.agents`, `.models`, `.tools` |
| `console.provision(wf)` | `management.apps.deploy(wf)` → `Deployment` |
| `console.ephemeral_app(wf)` | `management.apps.temporary(wf)` |
| `console.open_app(name)` | `management.apps.open(name)` → `ManagedApp`, `.client()` for a `DifyApp` |
| `DeployedApp.run(...)` | `managed.client().workflows.runs.create(...)` — and it is no longer gated on `DIFY_LIVE_TESTS`; that gate is on `run_live()` only |
| `client_for(key)` | `DifyApp(key)`. One class serves every mode; `DifyApp.open(key)` checks the mode first |
| `from dify_client.models import …` | gone; the typed results are `WorkflowRun`, `Message`, `NodeExecution`, `Dataset`, `Document`, … |
| `RunResult` / `NodeResult` | `WorkflowRun` / `NodeExecution`; the old names are aliases |
| `from dify_client.workflow.results import …` | `from dify_client.results import …` — the new path needs no `workflow` extra |
| `RunResult` | renamed `WorkflowRun`; the old name is an alias |
| `NodeResult` | renamed `NodeExecution`; the old name is an alias |
| `from dify_client.workflow.results import …` | `from dify_client.results import …` — the old path re-exports, but the new one needs no `workflow` extra |
| `stream_text(response)` / `stream_events(response)` | `with app.chat.messages.stream(…) as s: s.text()`, or iterate `s` for events |
| `StreamEvent` | `RunEvent`, which carries the same payload plus the node execution, the form token and the text |

### Known gaps

- Branching, iteration and loop nodes are still built with `wf.add()` and a
  graphon entity; only the common nodes have typed helpers.
- The knowledge surface has no async twin yet. `DifyApp` does.
- The compatibility story is capability probing rather than a version matrix,
  deliberately — see `probe()`. What that leaves open is history: this SDK is
  verified against Dify 1.17.1, DSL 0.7.0 and graphon 0.8.0, and nothing yet
  records how far back it works.
- No compatibility matrix yet for Dify, DSL and graphon versions. Everything
  here was verified against Dify 1.17.1 with DSL 0.7.0 and graphon 0.8.0.
- Branching, iteration and loop nodes still go through `wf.add()` with the
  graphon entity; only the common nodes have typed helpers.
