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
| `version.py` | `__version__` and the User-Agent. The version is read from the installed distribution, never written down twice |
| `sse.py` | decoding one server-sent event line. Internal; knows nothing about runs |
| `streams.py` | `WorkflowRunStream` / `MessageStream` — a run observed, built on `sse.py` |
| `compat.py` | capability probing |
| `workflow/`, `agent.py`, `skills.py`, `tools.py` | defining apps in code; needs the `workflow` extra |

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

uv run --with ruff ruff check dify_client tests
uv run --with black black --check dify_client tests
uv run --with isort isort --check-only dify_client tests
uv run --with mypy --with types-PyYAML mypy dify_client
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

The **billed** tests are gated separately and want a token rather than an email,
so they skip silently without one — which is how a billed test calling a deleted
method survived unnoticed:

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
- **Trigger nodes are not in graphon**, only in Dify's own `core.workflow.nodes`,
  so a workflow using one cannot run locally.
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
