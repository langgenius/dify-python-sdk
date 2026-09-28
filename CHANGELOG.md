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

- **`chunked_text()` gave the chunker a reference to nothing.** It read the
  extractor's `output`, which the extractor does not have — Dify's templates
  read `output` from a variable aggregator between the two. Run on Dify with
  both plugins installed, the pipeline indexed one chunk reading
  `tool.output` and reported success. It reads the extractor's `text` now, and
  a live test runs a file through the whole chain — upload, extract, chunk,
  index, search — and finds the document's text. (On a Dify whose
  `INTERNAL_FILES_URL` is unset, the extractor cannot fetch the upload and
  says so in its text; the test skips there, naming the setting.)

**Four from a sixth review.**

- **Confirming a held overwrite reported the caller's app as created.**
  `created` is what makes deleting an app safe. `confirm()` on either surface
  now claims it only when the held result shows a new app; given a bare
  import id, where there is no telling, it claims nothing. The console and
  `/openapi/v1` share one `confirm`.
- **An import whose answer never arrived could leave a temporary app
  behind.** With no answer there is no id, and cleanup deleted by id.
  `run_live()` and `apps.temporary()` now find the app by its exact name —
  one made unique for the purpose, so a name the caller chose is never swept
  — delete it, and say whether there was one. Checked on Dify, including that
  an app whose name only starts with ours is left alone.
- **An if-else case keyed `"false"` shared the else arm's handle**, so one of
  the two was never taken. graphon continues along `"false"` when no case
  matched; that key is refused.
- **`upload_for_pipeline(open("handbook.pdf", "rb"))` was refused** — the
  open file was sent as `document`, with no extension — and by then its bytes
  had been read, so a retry sent nothing. An open file goes up under its own
  name, and the name is checked before anything is read.

- **`/openapi/v1` raised over a held import and could not import a string.**
  `OpenApiApps.import_definition` now reads an import the way the console path
  does — one shared reading, so the two cannot drift again: a held import
  comes back with `needs_confirmation` and an `import_id`, a lost answer comes
  back `indeterminate`, and `OpenApiApps.confirm()` completes a held one. A
  string of DSL was handed to a method that called `.to_yaml()` on it and
  raised `AttributeError`; it is imported now. All three checked on Dify with
  a bearer minted for the test and revoked after it.

**Three from a fifth review.**

- **A node after one arm of a branch ran when the other arm was taken.** The
  node wired to the arm was left alone; one further down that also read the
  start node got an inferred edge from it, and Dify skips a node only when
  every way in was skipped. An inferred edge is now dropped when its target
  sits after a branch its source does not. Run on Dify: the arm not taken
  no longer runs, and an aggregator after both arms still rejoins them.
- **`weights=` on a knowledge node over several bases was never applied.**
  Dify merges them with the weights only when `reranking_enable` is set
  (`core/rag/retrieval/dataset_retrieval.py`), and it was left off — as
  Dify's own editor also leaves it. The node sets it; a knowledge base's
  stored settings, where hybrid search reads the weights regardless, do not
  change. Measured on Dify with a billed test: with the flag on, a 0.5 / 0.5
  weighting scored every chunk at exactly half the unweighted merge; with it
  off, the scores were identical to no weighting at all.
- **`body.until()` replaced the conditions `wf.loop(until=…)` was given.**
  They are added now. All of a loop's conditions share one operator, so a
  different `logical=` once there are conditions is refused.

**Three that imported, published and never ran,** from a fourth review. Each
was reproduced first, and the fixed shapes were run on Dify 1.17.1.

- **An edge inferred out of a branch was never taken.** Inference wrote every
  edge with the handle `source`, and a classifier, an if-else or a form only
  follows edges named after one of its arms. A node reading
  `kind["class_name"]` with no `connect()` passed `validate()`, and a local
  run reported `succeeded` with empty outputs. Nothing is inferred out of a
  branch now; `validate()` asks which arm leads to the node, and
  `wf.connect(branch, node)` with no arm — or with a name that is not one —
  is refused where it is written.
- **The human-input timeout arm was spelled `timeout`.** Dify's engine and
  editor both use `__timeout`, so the edge `approval()` drew for it was never
  followed and a form nobody answered stopped the run. `Branch.timeout` is
  that arm, and `case("timeout")` says to use it.
- **Only the first node in a container body ran from the start marker.** A
  second node reading `each.item` had no way in, and every pass returned
  `None` for it. The marker now leads to every body node nothing inside the
  body leads to, and `validate()` refuses any node with a way out and none in.

**Six more from the same round.**

- **A bare node was text but not a reference.** `wf.end({"answer": reply})`,
  `wf.knowledge(reply, …)`, `when(reply, …)`, `each.returns(node)` and the
  rest raised `AttributeError: 'Node' object has no attribute 'selector'`.
  Every argument that names a value takes a node for its output now, and
  anything else is a `TypeError` naming the argument. `loop_var()` and
  `assign()` wrote a node as a constant; they write it as a variable.
- **`wf.http(json={"q": start["q"]})` raised `TypeError`**, the one text
  argument of that node not rendered. A reference is written as the template,
  quoted — Dify substitutes it into the body text and then parses it.
- **A lost `confirm()` answer raised** instead of reporting `indeterminate`,
  the collapse `import_definition` was already guarded against. A refused
  confirm is reported as refused, not as still held.
- **`knowledge_index(retrieval=…, top_k=10)` dropped `top_k`**, the case its
  own check existed for. `top_k` and `score_threshold` are refused beside
  `retrieval=` now.
- **`hasattr(loop, "item")` raised** instead of answering `False`; the
  wrong-container error is an `AttributeError` as well as a `WorkflowError`.
- **`as_dsl()` is deleted.** Nothing called it, and it still rendered a node
  as its id.

**Nine from a third review, each reproduced before it was fixed.** They share
a shape: something the docstring promised that the code did not do.

- **A node in an f-string rendered as its id.** `Text` accepts a node so that
  `wf.answer(reply)` works, but `f"Reply: {reply}"` — the one place the SDK
  cannot intercept — emitted the literal `Reply: llm`. `Node.__str__` is the
  node's output now, the same as everywhere else it is accepted as text.
- **An inline Agent skipped the checks an Agent app gets.** "Needs a name" and
  "empty soul" lived in `Agent.to_dict()`, which a workflow shipping an inline
  Agent never calls; they live in `to_package()` now, which both go through,
  and the workflow's refusal names the binding that is wrong.
- **A `workflow` app accepted conversation variables** and wrote them into the
  DSL, where Dify has nowhere to keep them. Declaring one on a fixed
  `mode="workflow"` refuses immediately; on an app whose mode is not settled
  yet — it becomes a chatflow at `wf.answer(...)` — it is refused when the
  document is rendered, because absent is not the same as not yet determined.
- **A held import read as a refused one.** Dify answers a DSL version
  difference with `pending` and *keeps* the import, so `raise_for_stage()`
  said "Nothing was created on Dify" while a confirmable import sat on the
  server. Both `Deployment` and `PipelineDeployment` now carry
  `needs_confirmation` and `import_id`, and the refusal names `confirm(...)`.
- **`wf.knowledge(mode="single")` dropped `top_k` and `score_threshold`.** A
  single-mode node carries no retrieval config, so those numbers reached
  nothing and the search ran with the knowledge base's own settings. They are
  refused there now, the way `rerank=` and `weights=` already were.
- **`form_select` documented a variable-sourced option list and always wrote a
  constant**, so a selector became the dropdown's choices — two words instead
  of the array they pointed at. It takes a `VarRef` now, and a bare string is
  refused rather than split into one option per letter.
- **`datasets.create(embedding=…)` raised a bare `ValueError`**, outside the
  SDK's own hierarchy, so `except DifyClientError` missed the most likely
  failure there — a mistyped model reference. Both the sync and async twins
  raise `ValidationError`.
- **`grounded_answer(instruction=…)` ran `str.format` on caller text**, so an
  instruction showing the model a JSON example raised `KeyError` on the
  example's own braces. `{context}` and `{question}` are substituted and
  nothing else is touched; an instruction naming neither is refused, because
  the answer it produces would not be grounded.
- **The `loop()` docstring showed an example that raised `NameError`**, and
  the API it implied did not exist: `until=` is evaluated before the block
  runs, so it could only name nodes *outside* the loop. `body.until([...])`
  sets break conditions from inside the block, which is where a condition
  about the body has to be written — the same shape as `each.returns(...)`.

**Seven more from a second review, again reproduced first.**

- **A node handed to `wf.llm()` as the whole prompt hung the process.** The
  new `Text` alias let one through to `list(prompt)`, and `Node.__getitem__`
  answered `node[0]` with a reference rather than raising, so the iteration
  never ended. A node is a prompt now, and indexing one by position is a
  `TypeError` that names the fix — which protects everything else that might
  iterate one.
- **A lost publish response reported a definite failure.** `pipelines.deploy`
  now marks a transport error `indeterminate`, the way importing already did:
  the publish may have happened, and cleaning up on that assumption is worse
  than saying so.
- **`pipelines.list()` truncated at `MAX_WALK`** instead of raising
  `PageLimitReached`. A listing that quietly stops early is the bug `all()`
  exists to fix.
- **`pipelines.delete()` reported success on a refusal.** httpx does not raise
  on a 4xx, so a 403 left the knowledge base in place with nothing said.
- **`when(count, ">", 5)` raised a pydantic error** about a field the caller
  never mentioned. Dify's editor stores numbers as text, and so does this.
- **A pipeline input could belong to a processing node.** Dify fills in the
  inputs of the datasource being used, so one owned by anything else reads as
  unset at run time; it is refused at build time.
- **An edge written by hand can replace a derived one**, which the knowledge
  pipeline example did: connecting a chunker straight to its datasource left
  the extractor with nothing leading to it, so the chunker ran on nothing.
  `validate()` now refuses a graph where a node reads something no path leads
  to, and the example wires nothing by hand.

**Six fixes from a review of the builder, each reproduced before it was
changed.**

- `grounded_answer()` rendered a *node* handed to it as its id, so the prompt
  asked "Question: cleaned" instead of the text. The knowledge half resolved
  it and the prompt half did not; both go through one resolution now.
- `pipelines.list()` read one page of thirty knowledge bases and stopped,
  while promising every pipeline. Most bases are not pipelines, so the only
  one in a workspace could sit behind thirty that are not. It walks the pages,
  stopping at `MAX_WALK` like every other listing.
- `indexing="high_quality"` accepted a `retrieval=` block in place of an
  embedding model and wrote a base with neither. Search settings are not a
  place to store chunks; the model is required whatever else is passed.
- A container built inside another was written at the top level, which left
  the outer one reported as empty. A nested iteration or loop is parented like
  any other node.
- A container's start marker took `<id>start` without claiming it, so a node
  already called that produced two nodes under one id. The marker's id is
  claimed like any other and the container points at whatever it got.
- The knowledge-pipeline example and the README wired an extractor straight
  into the knowledge base, which queues a document and then fails indexing.
  Both now build the chain Dify's own templates use, and
  `recipes.file_pipeline` / `recipes.chunked_text` ship it.

**Running a knowledge pipeline was broken in two places**, both only
reachable once a pipeline could be built from code rather than by hand in the
console.

- `pipeline.datasources()` read the answer as `{"data": [...]}`. Dify sends a
  bare array there — it is a `RootModel[list[...]]`, unlike every other
  listing in the knowledge API — so the call raised `AttributeError` against a
  real pipeline. Both shapes are read now.
- `upload_for_pipeline()` sent every file under the name `upload`, with no
  extension. Dify picks a document reader by extension, so the upload answered
  200 and the document failed *indexing* with "Unsupported Extension Type: ."
  long after the call returned. The path's own name is kept, and a file with
  no extension is refused before it is sent.



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
  measured whatever was published before. It now runs the draft it imported,
  in a temporary app it deletes afterwards, and publishes nothing. (A first
  fix published over the named app, which replaced what that app's users were
  served with the code under test.) `app_id=` runs an existing app's draft and
  leaves its published version alone; `apps.run_draft()` is the underlying
  call. Checked on Dify: a named app's draft run answered with the new
  definition while the Service API still served the old one.
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

**Every node type Dify serves, with a typed helper.** `wf.knowledge`,
`wf.if_else`, `wf.http`, `wf.classify`, `wf.extract_parameters`,
`wf.extract_text`, `wf.aggregate`, `wf.assign`, `wf.list_operator`,
`wf.iteration`, `wf.loop`, `wf.human_input`, `wf.agent`, `wf.dify_agent`,
`wf.inline_agent`, `wf.datasource`, `wf.knowledge_index` and
`wf.plugin_trigger`, alongside the ones that were already there. Each was
imported into a running Dify 1.17.1 and published before it was written down.

- `when(...)` and `of_file(...)` build conditions, checking the operator
  against graphon's own list — Dify spells inequality `≠`, and `!=` imports
  fine and then never fires.
- `bearer`, `basic` and `api_key` build HTTP credentials; `parameter()` builds
  an extractor field; `action()`, `form_paragraph()`, `form_select()`,
  `form_file()` and `form_files()` build a human-input form.
- `wf.iteration()` and `wf.loop()` are context managers: nodes built inside the
  block belong to the container, which is what `parentId` in the DSL says and
  what makes Dify draw them inside the box.
- `wf.conversation_var()` declares a variable that survives between the
  messages of a chatflow, which `wf.assign()` writes to.
- `knowledge=StubKnowledge([...])` answers knowledge nodes in a local run, the
  way `StubLLM` answers model nodes. The chunks it returns carry the keys a
  live 1.17.1 run sends, so a workflow that indexes them keeps working once
  deployed. Retrieval quality is the server's, not the stub's.
- A run that contains a node graphon cannot execute now fails saying which type
  and what to do about it, rather than "Unsupported node types".
- Two ways to put a Dify Agent in a workflow, which are not the same thing.
  `wf.dify_agent(agent)` binds a published Agent the workspace already has —
  shared, and republishing it changes every workflow bound to it.
  `wf.inline_agent(agent)` ships a `dify_client.Agent` inside the document
  under `agent_packages`, and Dify creates an Agent owned by that node, so the
  workflow is self-contained. `declared_output()` names what either must
  answer with, and `Agent.to_package()` is the shape both routes export.
- `body.stop()` inside a loop is Dify's loop-end node: `until=` is checked
  between passes, this leaves during one.

**Embedding and reranking, where the knowledge base keeps them.**
`datasets.create(embedding=…, retrieval=…)` on both the sync and async
knowledge clients, and the same two on `wf.knowledge_index(...)`, so a base
built here searches the way it was meant to rather than on Dify's defaults.

- `retrieval_model()` builds the block, and `weighted_score()` the other
  reranking mode. A rerank model and a weighted score are refused together:
  Dify runs one or the other.
- A score threshold sets the flag that makes Dify read it. Passing no
  threshold leaves the filter off, and those are two states, not one.
- `wf.knowledge(..., weights=...)` gives the knowledge-retrieval node the same
  choice.
- A billed live test indexes the same chunks into two knowledge bases and
  compares the scores, which is the only way to tell a rerank model that is
  configured from one that is consulted.

**Knowledge pipelines, built and deployed like workflows.** `Pipeline` writes
a `kind: rag_pipeline` document — a datasource at the front, a knowledge base
at the back, ordinary workflow nodes in between — and
`DifyManagement.pipelines` imports, confirms, publishes, exports, lists and
deletes one.

- A pipeline carries its own DSL version (`0.1.0`, not the app's `0.7.0`);
  sending the app's makes Dify hold the import for confirmation.
- `pipe.variable(datasource, name)` declares an input, which is a three-part
  reference (`{{#rag.<node>.<name>#}}`) and a form in Dify's UI. `VarRef` grew
  an optional third segment for it. `pipe.variable(None, name)` declares one in
  the `shared` scope instead, which is where Dify's own templates keep the
  chunking settings every datasource uses.
- A deploy answers with `PipelineDeployment`, which keeps `pipeline_id` and
  `dataset_id` apart: the knowledge base owns the pipeline, so deleting the
  base is what deletes both, and Dify names the base after the pipeline *plus a
  number*.
- `wf.knowledge_index()` always writes the search settings Dify validates the
  node against; without them the import fails on a field the DSL never
  mentions.
- `examples/10_knowledge_pipeline.py` builds one, prints what it would deploy,
  and deploys it when the live gate is open — which costs nothing, because
  nothing is indexed.

**Edges are derived from the references that already imply them.** A node
built with `variables={"n": start["name"]}` has said it runs after `start`;
writing `wf.connect(start, greet)` as well was bookkeeping the SDK could do
itself, and forgetting it was the most common way to build a workflow that
imports and does nothing.

- `wf.connect` is now for control flow: which arm of a branch to take. Arms
  name themselves — `branch.true`, `branch.false`, `kind.case("refund")`,
  `gate.case("approve")` — so a handle is never a typed string.
- `wf.merge(a, b)` builds the variable aggregator that rejoins branches, which
  the examples previously told you to remember.
- A node stands for its own output where text is wanted, so `wf.answer(reply)`
  reads as well as `wf.answer(reply.output)`.
- `wf.edges` shows what will be sent. Two rules keep the derivation honest,
  and both were bugs first: a node whose inbound edges were written by hand is
  left alone, and nothing is inferred across a container boundary.

**Recipes: the shapes most apps turn out to be.**
`dify_client.workflow.recipes` ships `rag_answer` — a deployable chatflow that
answers from a knowledge base — and the fragments `grounded_answer`,
`extract_fields` and `approval`, which add their nodes to a workflow you own
and hand back what to read next. Each is deployed to a real Dify by a test,
and the grounding prompt is part of the recipe rather than left to the caller.

**One graph, two documents.** `GraphDocument` holds what an app and a
knowledge pipeline share — the nodes, the edges, the canvas, the environment
variables, and every node helper that is not an end — and `Workflow` and
`Pipeline` each add their own ends, envelope and `validate()`. A pipeline no
longer inherits `start()`, `answer()`, `end()`, the triggers,
`conversation_var()` or `run_live()`, none of which it could have used: an
answer node in a pipeline used to build and validate cleanly.

- `Iteration` and `Loop` replace a single `Container` with a `kind` flag.
  `item` / `index` / `returns` belong to one and `var` / `stop` to the other,
  so the type says which, and reaching for the wrong one still names the fix.
- `dify_client.workflow.nodes` holds one module per node family — `agents`,
  `human_input`, `http`, `knowledge`, `logic`, `models`, `tools`, `triggers` —
  carrying both the schema, where this SDK owns it, and what Dify accepts of
  that node type. The builder's helpers are their signatures, their
  documentation and a call; the rules moved to the node they are about, and
  can be tested without a document. `dify_client.workflow.local_knowledge`
  keeps the stand-in that runs a knowledge node without a server.
- The model reference splitter, the node-argument classifier and the
  deploy-stage rules each had two copies; each has one. `AgentInput` is
  `NodeInput`, because a datasource and a trigger take the same shape.
- `retrieval_model()` is the single place that decides what reranking means;
  the knowledge node validates its output rather than deciding again.
- `wf.dify_agent()` and `wf.inline_agent()` are an app's. A pipeline accepted
  them, published, and dropped the agent package on the way — a node bound to
  something that was no longer in the document. `wf.agent()` needs no binding
  and stays on both.
- `body.until([...])` on a loop, `Apps.confirm(...)` for an import Dify held
  over a DSL version difference, and `NodeError` exported — a field builder
  called on its own, such as `form_select`, raises it, so it has to be
  nameable. `form_select` and `form_paragraph` both take a `VarRef` now.

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

- The compatibility story is capability probing rather than a version matrix,
  deliberately — see `probe()`. What that leaves open is history: this SDK is
  verified against Dify 1.17.1, DSL 0.7.0 and graphon 0.8.0, and nothing yet
  records how far back it works.
- Indexing *through* a pipeline needs two marketplace plugins —
  `langgenius/dify_extractor` and `langgenius/general_chunker` — and this SDK
  cannot install them. With them installed, `file_pipeline()` indexes a file
  end to end; the plugin runtime also needs Dify's `INTERNAL_FILES_URL` set to
  fetch the upload.
