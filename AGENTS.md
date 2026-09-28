# Working on dify-python-sdk

Instructions for coding agents. `CLAUDE.md` points here; keep this the only copy.

## What this is

A Python SDK for Dify, with two contracts that are kept separate:

- **Using Dify** — `DifyApp`, `DifyKnowledge`, `DifyManagement`. Needs nothing
  but `httpx`.
- **Defining Dify apps in code** — `dify_client.workflow`, `dify_client.agent`.
  Needs the `workflow` extra, which pulls in `graphon`.

Nothing in the first may import from the second. `dify_client/results.py` and
`usage.py` live outside `dify_client/workflow/` for exactly this reason: the
result types describe a run's outcome, the Service API reports one, and
importing them must not require `graphon`. There is a test that enforces it.

## Where things are

| | |
|---|---|
| `base_client.py` | retries, error mapping, `None`-param stripping — shared by both transports |
| `_transport.py`, `_async_transport.py` | the clients. Private: they hold a connection, not a vocabulary |
| `app.py`, `knowledge.py`, `console.py`, `openapi.py` | the entry points |
| `resources/` | the verbs, grouped by what they act on |
| `results.py`, `usage.py`, `lifecycle.py` | what calls return. No `graphon` import, ever |
| `paging.py` | turning Dify's two paging styles into one `Page` / `AsyncPage` |
| `catalog.py` | model providers and models, shared by the Service API and the console |
| `search.py` | how a knowledge base is searched — one retrieval block, built once for the three places that take it |
| `version.py` | `__version__` and the User-Agent. The version is read from the installed distribution, never written down twice |
| `sse.py` | decoding one server-sent event line. Internal; knows nothing about runs |
| `streams.py` | `WorkflowRunStream` / `MessageStream` — a run observed, built on `sse.py` |
| `compat.py` | capability probing |
| `workflow/`, `agent.py`, `skills.py`, `tools.py` | defining apps in code; needs the `workflow` extra |
| `workflow/graph.py` | the graph both documents are: nodes, edges, canvas, and every node helper that is not an end |
| `workflow/parts.py` | what a graph is assembled from — variables, edges, containers, arguments — each carrying the one rule Dify would report late |
| `workflow/builder.py`, `workflow/pipeline.py` | the two documents — an app (`kind: app`) and a knowledge pipeline (`kind: rag_pipeline`). Each adds its own ends, its own envelope and its own `validate()` |
| `workflow/nodes/` | one module per node family — the schema when this SDK owns it, and in every case the rules Dify applies to that node type. A rule about retrieval modes is a fact about the knowledge node, not about documents, so it lives here and the helper delegates |
| `workflow/local_knowledge.py` | the knowledge node run without a server: a stand-in, not a re-implementation |

## The rule that matters most

**Verify against a running Dify. Do not trust the documentation, and do not
trust this file.**

The SDK this replaced was written against Dify's docs and an older server. It
had ~1,100 lines of classes calling endpoints Dify has never served, seventeen
methods pointing console paths at the Service API, and size limits Dify does
not impose. Every one of those passed a green test suite for years, because the
tests mocked the server.

There is a Dify checkout on this machine at `../dify-oss` (a sibling of this
repo). Read the controllers; they are the specification:

```
../dify-oss/api/controllers/service_api/     # /v1   — app keys
../dify-oss/api/controllers/console/         # /console/api — account session
../dify-oss/api/controllers/openapi/         # /openapi/v1  — dfoa_ bearers
../dify-oss/api/controllers/common/          # shared request/response models
```

To find what a route really accepts, read its pydantic query or payload model.
Several bugs found this way: `get_conversations` sent a `pinned` filter Dify
silently ignores (so the "filtered" list was the whole list), and conversation
variables were paged with `page` where Dify wants `last_id`.

When a claim cannot be checked against a real server, say so rather than
implying it was.

## Commands

```bash
uv run pytest                  # everything; the live harness skips itself
uv run pytest -m "not live"    # unit tests only — what CI runs on every push
uv run pytest tests/live       # the live harness (needs a Dify, see below)

uv run ruff check dify_client tests
uv run ruff format --check dify_client tests
uv run mypy dify_client
uv build && uv run --with twine twine check dist/*
```

All of those must pass before calling work done. `.venv/bin/python` is the
interpreter, and there is no `pytest` on PATH — `uv run` is how anything gets
run here. The repo has no `pip` either, so reach for `uv run --with <tool>` for
anything not already installed.

### The live harness

```bash
set -a && . ./.env && set +a     # the local instance's credentials
uv run pytest tests/live
```

The **billed** tests spend money, so they also want `DIFY_LIVE_TESTS=1`. The
ones under `tests/live/` ask for nothing else — the harness logs in when they
run:

```bash
DIFY_LIVE_TESTS=1 uv run pytest tests/live -m billed
```

`tests/test_workflow_billed_example.py` is different on purpose: it is the
sample of a *user's* billed test, written with `requires_live` and
`DifyManagement()`, which read a console token from the environment because a
user's test has no other way in. Without one it skips silently — which is how
a billed test calling a deleted method survived unnoticed. To run it:

```bash
eval $(uv run python -c "
import os
from dify_client import DifyManagement
c = DifyManagement.login(os.environ['DIFY_CONSOLE_EMAIL'], os.environ['DIFY_CONSOLE_PASSWORD'])
print(f'export DIFY_CONSOLE_TOKEN={c.token}')
print(f'export DIFY_CONSOLE_CSRF_TOKEN={c._csrf_token}')")
DIFY_LIVE_TESTS=1 uv run pytest -m billed
```

A skipped billed test proves nothing. Run it before saying an example works.

Credentials for the local instance are in `.env` (gitignored). `tests/live/`
costs nothing to run, creates what it needs under the `sdk-harness-` prefix,
and sweeps leftovers from crashed runs. See `tests/live/README.md`.

A Dify that lacks a feature is a supported Dify: use the `needs("capability")`
fixture to skip rather than fail. `dify_client.compat.probe()` is what answers,
and it is a library feature, not test scaffolding.

## The vocabulary

The API is arranged around Dify's own nouns. Adding a verb means finding the
noun it acts on, not adding a method to a client.

- A **client** holds the connection and the credential and nothing else.
  `_transport.py` / `_async_transport.py` are private for that reason.
- A **resource** holds the verbs for one kind of thing:
  `app.workflows.runs`, `app.chat.messages`, `management.apps.keys`.
- A call returns **the thing**, never an `httpx.Response`, carrying the ids the
  next call needs. `run_id`, `task_id` and `node_id` address different things
  and are never flattened into `id`.
- A **stream** is how a run is observed. It yields typed events and then
  answers `get_final_run()` / `get_final_message()`.

Three entry points exist because Dify scopes three credentials. Do not add a
method that needs a different credential than its client holds; add it to the
client that has it, and say so in the error if someone reaches for the wrong one.

### Distinctions to preserve

These were each a bug once. Collapsing any of them reintroduces it.

| Not the same as | |
|---|---|
| a node | one execution of it — a node in a loop has one per pass, and usage is summed over executions |
| importing | publishing — import writes a draft, and the Service API runs the *published* version |
| closing a stream | stopping the run |
| a paused run | a failed one — `paused`, `failed` and `succeeded` are three questions |
| a dropped connection | anything about the run |
| HTTP 200 | a successful run — Dify reports mid-stream failures as an event after the status line |
| absent | not determined — `compat` answers three ways, and unknown is not permission to proceed |
| a timeout | a 429 — a timeout may already have been acted on and is not retried for POST, a 429 was refused and is waited out for any method |
| a published pipeline run | a draft one — published enqueues documents and answers with a batch, draft executes the graph and reports a `WorkflowRun` |
| a page that continues from its last item | one that continues from its first — conversations page newest-first (`last_id`), message history oldest-first (`first_id`), and taking the wrong end silently re-reads the page |
| a reported cost of zero | no cost reported — and an observed node price outranks an aggregate zero, or a billed run reads as free |
| a pause | a form token — Dify leaves the token out of a form meant for its own UI, and names the *node* on every human-input event |
| publishing a workflow | publishing an Agent — `/apps/<id>/workflows/publish` serves `workflow` and `advanced-chat` only; an Agent publishes at `/agent/<agent id>/publish`, keyed by its roster id |
| a field you defined | one of Dify's own — a built-in metadata field has no id, because it is turned on rather than managed |
| `Page` | `AsyncPage` — same fields and verbs, but one blocks and one is awaited, and a single type doing both is a trap |

### A listing must always terminate

`Page.all()` walks until the server says stop, so the server is trusted with a
loop. Two things bound it, and anything new that walks pages needs both: a
cursor that comes back unchanged ends the walk (asking again returns the same
page), and the walk stops at `MAX_WALK` items with `PageLimitReached` rather
than trusting `has_more` for ever. It raises rather than truncating — a walk
that quietly stops early is the bug `all()` was written to fix.

### Sync and async are the same API

Every verb on `DifyApp` and `DifyKnowledge` has an async counterpart with the
same name, the same parameters and the same return shape — `close` is the one
exception, spelled `aclose`. `tests/test_async_is_not_second_class.py` asserts
it by reflection, so adding a sync method without its counterpart fails the
suite. Async was a subset for a long time and callers found the holes.

Listings return `Page` on the sync path and `AsyncPage` on the async one; build
them with `paging.by_page` / `by_cursor` / `unpaged` (and the `_async` twins)
rather than constructing a page by hand, or the new listing will be the one
that cannot fetch its own next page.

### What gets a type and what stays a dict

Type what a caller must read to make the *next* call — the inputs a run takes,
the model to configure a base with, the tag to bind. Leave a `dict` where
Dify's shape is open-ended and keeps growing (`meta`, run logs, datasource
descriptors): naming those fields is a promise this SDK cannot keep. Every type
carries the whole answer on `.payload`, so nothing is lost either way.

Two things the shaping is there to absorb: `binding_count` on a tag arrives as
a *string*, and a label is `{"en_US": …, "zh_Hans": …}` rather than text.

## Things about Dify that are easy to get wrong

Each of these cost real debugging time.

- **An Agent app is not live when it imports.** It has no workflow draft, so
  `/workflows/publish` refuses it — but minting a key before publishing it on
  the roster answers "Publish the Agent before enabling Web App or API
  access". `apps.deploy()` reads the mode Dify *reports* (`imported.app_mode`)
  rather than the definition's, because the same Agent can be passed as an
  object or as DSL text, and text has no `.mode`.
- **Message history pages by `created_at`, stored to the second, and ties are
  excluded.** Messages written inside the same second can be skipped by any
  client paging `/messages` — the server drops them from the cursor, so this is
  not something an SDK can fix. Read short conversations in one page.
- **`message_replace` carries a whole answer, not a chunk.** Output moderation
  replaces what was streamed; appending it hands back the rejected text with
  the replacement stuck on the end. What Dify saves is the replacement.
- **A run can report `status: "paused"` with no forms attached.** The form
  tokens are raised on the stream; a run read back with `retrieve()` has only
  the word. A blocking run that pauses carries them under `data.reasons`.
- **Service-API keys are reveal-once**, and a workspace holds ten dataset
  keys. There is no reading one back — the listing masks the token, and a
  masked token authenticates as "Access token is invalid". Mint one, use it,
  revoke it; leaking them fills the cap and the error never says so.
- **`/workspaces/current/models/model-types/…` takes the dataset token**, not
  an app key — the one Service-API route that does. It is on `DifyKnowledge`
  for that reason. When a route 401s with a key that works elsewhere, read
  the controller's decorator before believing the key is wrong.
- **Console writes need a CSRF token** since Dify 1.17, alongside the access
  token. An access token alone can read but not deploy — and the failure is a
  flat 401. `DifyManagement.login()` collects both.
- **App mode decides the route.** A chatflow is served at `/chat-messages` and
  needs a `query` *and* its start inputs; a workflow app runs on inputs alone at
  `/workflows/run`. The wrong route answers "check if your app mode matches".
- **A document is not searchable when it uploads.** Wait for indexing.
- **How a knowledge base is searched is stored on the base**, not passed per
  call, so `datasets.create(embedding=…, retrieval=…)` decides what every
  later retrieval does — a workflow's knowledge node included. Three things
  about that block are easy to get wrong:
  - **A score threshold is two fields.** `score_threshold` is ignored unless
    `score_threshold_enabled` is true, so a threshold set alone reads as a
    filter that does nothing. `retrieval_model(score_threshold=…)` sets both,
    and omitting it turns the flag off — those are the two states.
  - **An `economy` base is always searched by keyword.** `dataset_retrieval`
    overrides `search_method` when the indexing technique is economy, because
    there are no embeddings to compare against. The setting is still stored,
    and starts mattering if the base is switched to `high_quality`.
  - **Reranking has two modes and only one calls a model.** Dify reads
    `reranking_model` only when `reranking_enable` is true; `weighted_score`
    blends the vector and keyword scores arithmetically. Setting both is not a
    stronger rerank, it is a contradiction — `retrieval_model()` refuses it.
  - **What `reranking_enable` means depends on where it is written.** On a
    knowledge base's own settings, hybrid search reads the weights whatever
    the flag says, so `retrieval_model(weights=...)` leaves it off. On a
    workflow's knowledge node over several bases it is the switch: the merge
    uses the weights only `if reranking_enable and dataset_count > 1`
    (`core/rag/retrieval/dataset_retrieval.py`), and **Dify's own editor
    writes it off**, so a weighted node built in the UI merges unweighted.
    The node path sets it. Measured on 1.17.1 with vector 0.5 / keyword 0.5
    and a query sharing no keyword with the chunks: flag on scored every
    chunk at exactly half the unweighted run; flag off scored identically to
    it. `test_billed_rerank.py` pins both.
- **Some node types are not in graphon**, only in Dify's own
  `core.workflow.nodes`, so a workflow using one cannot run locally:
  `knowledge-retrieval`, `knowledge-index`, `document-extractor`,
  `human-input`, `agent`, `datasource` and the three triggers. What *does* run
  is `graphon.dsl.node_factory.SUPPORTED_DEFAULT_FACTORY_NODE_TYPES` — read it
  rather than a list written down here. The knowledge node is the one with a
  local stand-in (`knowledge=StubKnowledge([...])`), because retrieval has a
  seam a fixture can sit in; the rest do not.
- **Dify encrypts `dataset_ids` when it exports a workflow**, keyed by the
  tenant, and falls back to a plain UUID when it imports one. So a knowledge
  node built here imports fine, an export does not name the knowledge base, and
  an export imported into *another workspace* silently loses it — the id
  decrypts to nothing and is filtered out, leaving a knowledge node with no
  knowledge base and no error. `app_dsl_service.decrypt_dataset_id` is the
  fallback; `DSL_EXPORT_ENCRYPT_DATASET_ID` is the switch.
- **The agent node is two nodes, and the second one binds two ways.** A plugin
  *strategy* (`wf.agent(...)`) publishes even when the plugin is missing,
  because Dify resolves the strategy at run time — publishing is not the check
  it looks like. A *Dify Agent* (version `2`) names an Agent through
  `agent_binding`, and Dify writes the binding record while importing the
  draft: `roster_agent` + `agent_id` points at a published workspace Agent
  (`wf.dify_agent`), and `inline_agent` + `package_ref` ships the Agent inside
  the document under `agent_packages`, which Dify materializes as an Agent
  owned by that node (`wf.inline_agent`). Three failures, and they land in
  three different places: an unpublished roster Agent fails the **import**, an
  empty `agent_binding` fails the **import**, and a binding naming a kind but
  no id is skipped on import and fails the **publish** with "requires a
  binding before publishing". The job config also moves: a roster node carries
  `agent_task` / `agent_declared_outputs`, a packaged one carries the same two
  inside `agent_job`.
- **`datasource` and `knowledge-index` are pipeline nodes an app will take.**
  They belong to `kind: rag_pipeline`, but an app workflow carrying one
  imports and publishes — checked against 1.17.1 rather than assumed from
  where they live.
- **A pipeline drops what its importer does not know, and publishes anyway.**
  An agent node bound to an inline package imports, publishes, and comes back
  exported with `agent_packages: []` — the binding still names a package that
  is no longer there, and nothing fails until the run. `rag_pipeline_dsl_service`
  never calls `sync_agent_bindings_for_draft` or `AgentDslService`, so neither
  agent-v2 binding is backed by a record there. `wf.dify_agent()` and
  `wf.inline_agent()` are therefore on the app document only; `wf.agent()`, a
  plugin strategy that needs no binding, is on both.
- **A knowledge pipeline is not an app, and almost nothing about it is
  shared.** It imports at `/rag/pipelines/imports`, publishes at
  `/rag/pipelines/<id>/workflows/publish`, and its DSL is `kind: rag_pipeline`
  at **version 0.1.0** — sending the app's `0.7.0` makes Dify hold the import
  for confirmation, which reads like a failure and is a second round trip.
  Four more things it does not share:
  - **The import payload's `name` is accepted and never read.**
    `import_rag_pipeline` takes `dataset_name` and does nothing with it; the
    knowledge base is named from the document's `rag_pipeline.name`.
  - **That name always gets a number appended.**
    `generate_incremental_name` returns `"<name> 1"` even when nothing
    collides, so a pipeline called `support-docs` fills a base called
    `support-docs 1`. Address the base by the `dataset_id` the import
    returns; looking it up by name finds nothing.
  - **The knowledge-index node is validated as a `KnowledgeConfiguration`,
    which requires `retrieval_model`.** Without it the import fails with a
    pydantic error about a field the DSL never mentions. `wf.knowledge_index`
    always writes one.
  - **A knowledge-index node takes chunks, not text.** Dify validates what
    reaches it as a structured chunk, so an extractor wired straight into it
    queues the document and then fails *indexing* — "Input should be a valid
    dictionary or instance of MultimodalGeneralStructureChunk". Chunks come
    from a chunker plugin (`langgenius/general_chunker` after
    `langgenius/dify_extractor` in Dify's own templates), which is a tool node.
  - **A pipeline input belongs to a datasource, or to `shared`.**
    `belong_to_node_id` is the datasource whose form shows it, or the literal
    `shared`, which is where Dify's templates keep the chunking settings every
    source uses. Dify fills in the inputs of the datasource being used and
    nothing else, so one owned by a processing node is read as unset at run
    time rather than refused at import — the builder refuses it instead.
  - **There is no route that deletes a pipeline.** The knowledge base owns it:
    `DELETE /datasets/<dataset_id>` removes both. A pipeline has no listing of
    its own either — it is reported by the dataset rows that carry a
    `pipeline_id`.
- **The running version is at `GET /v1/`** — the Service API's own index, no
  credential, always served, `server_version` in the body. Two places look like
  they would answer and do not: `/openapi/v1/_version` needs `OPENAPI_ENABLED`,
  and `/console/api/version` reports the *latest released* version, which is a
  confidently wrong answer rather than a missing one. A version still says
  little about what a server can do, since features are turned off
  individually — probe capabilities for that.
- **`None` in a query parameter** becomes `?limit=` on the wire, which Dify's
  typed query models reject. The transport strips them. `None` in a *body* is a
  value — `rating=None` revokes feedback — and is left alone.
- **Dify deprecates six Service-API routes, and the naming is not a clue.**
  The *hyphenated* `create-by-text` / `create-by-file` / `update-by-text` are
  canonical; the underscored ones are `DeprecatedDocumentAddByTextApi` and
  friends. Both spellings of `update-by-file` are deprecated — `PATCH
  /documents/<id>` replaces them. `/hit-testing` is the deprecated alias of
  `/retrieve`, on the same Resource. Everything else Dify serves is current.
  `test_service_api_coverage.py` reads this out of Dify's decorators with
  `ast` — a regex over the source missed seven operations, four of them the
  current child-chunk API, which therefore looked covered when nothing was
  checking it.
- **A current route can carry a deprecated field.** `TagUnbindingPayload`
  takes a singular `tag_id` and marks it deprecated in its own JSON schema
  while still accepting it, so sending it works and nothing says otherwise.
  The same test reads those out too.
- **Retrying is not always safe.** A `ReadTimeout` means the request was sent;
  retrying `POST /workflows/run` can bill the run again. Only connect-phase
  failures and idempotent methods are retried.

## Conventions

**Comments say why, never what.** The code says what. A comment earns its place
by recording something the reader cannot see: a Dify behaviour, a bug this
shape prevents, a decision and its alternative.

**Tests are named as sentences** describing the behaviour, and their docstrings
say what breaks without them — `test_a_post_is_not_retried_once_it_has_been_sent`,
not `test_retry_2`. A test asserting something that was once broken should say
so, so the next person does not "simplify" it away.

**Delete rather than deprecate.** This package is not widely used and its
history is an old Dify; carrying dead shapes forward has cost more than it
saved. When deleting, leave a test that keeps it deleted.

**Errors name the fix.** `"No app called 'x'. console.apps.list() shows what is
there."` beats a bare 404. If a failure has a known cause, say it.

**Breaking changes go in `CHANGELOG.md`** with a row in the Migrating table
mapping the old spelling to the new.

## What not to do

- Do not mock the server and call it verified.
- Do not add a method for an endpoint without reading its controller.
- Do not widen a public signature to paper over an internal one.
- Do not add a compatibility shim for the pre-1.0 API. It is gone on purpose.
- Do not leave apps behind on the shared Dify. Name them, delete them.
