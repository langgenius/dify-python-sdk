# Deploying

## Which API

Dify has two, and this SDK speaks both.

| | `DifyManagement` | `OpenApiClient` |
|---|---|---|
| Path | `/console/api` | `/openapi/v1` |
| Meant for | the web console | programmatic clients, including `difyctl` |
| Credential | a console session (`DIFY_CONSOLE_TOKEN`) | a scoped OAuth bearer (`DIFY_TOKEN`) |
| Available | always | only when the operator enables it |
| Publish | yes | **no** |

`OpenApiClient` is the better surface where it exists — the credential is scoped
and expiring, and one `:run` path serves every app mode. It is off by default
twice over: `OPENAPI_ENABLED` must be true, and the caller's `client_id` must
appear in `OPENAPI_KNOWN_CLIENT_IDS`, which ships holding only `difyctl`. So
`DifyManagement` is the path that works anywhere.

```python
from dify_client import DifyManagement, OpenApiClient

console = DifyManagement.login(email, password, base_url=host)
if OpenApiClient.available(host):
    api = console.open_api(client_id="difyctl")
    result = api.deploy(wf)
else:
    result = console.apps.deploy(wf)
console.apps.publish(result.app_id)      # publish is console-only either way
```

## Environment variables

| Variable | What it is | Read by |
|---|---|---|
| `DIFY_HOST` | the Dify host | everything; `difyctl` reads it too |
| `DIFY_API_KEY` | an app's Service-API key (`app-…`) | `ChatClient` and friends |
| `DIFY_CONSOLE_TOKEN` | a console session | `DifyManagement` |
| `DIFY_TOKEN` | a `dfoa_` OAuth bearer | `OpenApiClient`; `difyctl` reads it too |

`DIFY_HOST` alone configures a self-hosted Dify: the Service API base is derived
as `<host>/v1`. `DIFY_TOKEN` and `DIFY_CONSOLE_TOKEN` are **not**
interchangeable — different credentials for different APIs — and handing a
`dfoa_` token to `DifyManagement` is refused with an explanation.

A console token is a full-privilege account credential; Dify has no
deploy-scoped equivalent on that surface. Where a job only needs to *run* an
app, use the app's `app-…` key, which is scoped to that app.

## Getting a bearer without a browser

The device flow normally sends a person to a browser. With a console session in
hand, this SDK approves it directly:

```python
token = console.mint_openapi_token(client_id="difyctl")
```

An unregistered `client_id` is refused naming the setting the operator must
change, rather than a bare `unsupported_client`.

## App keys

Reveal-once and capped per app: Dify returns the whole token only when a key is
created, and a listing shows a masked form. Create one and keep it rather than
minting one per run.

```python
key = console.apps.keys.create(app_id)     # key.token — the only look
console.apps.keys.list(app_id)            # masked; identifies, cannot authenticate
```

## Overwriting

`deploy(wf, app_id=...)` overwrites that app, which is what keeps code the
source of truth: a UI edit is replaced on the next deploy. Agents are the
exception — Dify's importer only creates new Agent apps — and passing `app_id`
for one is refused before the request.

## Running a deployed app

```python
app = console.apps.deploy(wf)                  # import + publish + key in one call
app.workflows.runs.create({"name": "Dify"})                    # workflow
app.workflows.runs.create({"message": text}, query=text)       # chatflow: inputs AND query
```

A chatflow needs both: the query fills `sys.query`, and the start node's own
variables are still required. Omitting either fails with
`message is required in input form` or `query_required_for_chat`.

## Agents

An Agent's configuration — its *soul* — is defined by models inside the Dify
server that are not published as a package, unlike workflow nodes which come
from `graphon`. `Agent` therefore carries the soul as plain data it does not
validate.

### Building one

`Agent.create()` starts from the soul Dify itself writes for a new Agent — every
section, every default — and fills in what most Agents need:

```python
from dify_client import Agent, DifyManagement, dify_tool

catalog = console.tools.catalog()
agent = Agent.create(
    "support-triage",
    instruction="Decide whether an incoming message needs paging.",
    model="langgenius/openai/openai:gpt-4o-mini",
    tools=[dify_tool(catalog["time"]["current_time"])],
    role="On-call triage",
    temperature=0.2,
)
agent.soul["memory"]["scope"] = ...      # anything the helpers do not cover
console.apps.deploy(agent)
```

`agent.instruction`, `agent.model`, `agent.use_model()` and `agent.add_tool()`
reach the common fields; `agent.soul` is a dict for everything else. A field
name this SDK cannot check is caught by Dify on import, not here.

An import answers `completed-with-warnings` when a tool needs re-authorising —
a portable package carries no credential bindings, by design:

```
agent_tool_authorization_required: Agent tool 'current_time' requires authorization.
```

### Skills

A Dify skill is a zip holding a `SKILL.md` whose YAML frontmatter names it and
says when to use it — the same shape a Claude Code skill has, so a directory
written for one usually serves as the other. Dify requires:

| Field | Rule |
|---|---|
| `name` | lower-case words joined by hyphens, up to 64 characters |
| `description` | 1–1024 characters; it is what tells the Agent when to reach for the skill |

```python
from dify_client import Skill

skill = Skill.from_directory("skills/paging-policy")   # validated offline
installed = console.skills.create(skill)                # uploads, then publishes
agent.use_skill("paging-policy")                       # bound by name
```

`import_skill` publishes by default: Dify stores an import as a draft, and an
Agent binds to a *published* version — the same trap workflows have.

Two kinds of skill exist. A **workspace skill** lives in the workspace and
Agents bind to it by name; this is what travels in an exported Agent, as
`workspace_skills`. A **config skill** is an archive attached to one Agent, and
an export records only that it was there, because the bytes cannot travel in a
DSL. `use_skill` binds the portable kind.

`console.skills.list()` lists what is installed, and `console.skills.delete(skill)`
removes one — Dify asks for the name back as confirmation, since an Agent bound
to a deleted skill loses it.

Packaging is deterministic, so `skill.digest` tells versions apart without
uploading.

### Editing an existing one

Export, edit, redeploy. Preferable when the Agent was configured in the UI,
because the export is known-good:

```python
agent = Agent.from_yaml(console.apps.export(app_id))
agent.instruction += " When in doubt, do not page."
agent.to_yaml("support-triage.yml")      # commit; credentials already stripped
```

### Finding them

Agents live on their own roster and appear in **neither**
`/console/api/apps` nor `OpenApiClient.apps()`:

```python
console.agents.list()                  # AgentSummary(name, app_id, role, published)
console.agents.retrieve("support-triage")   # by name
```

`app_id` is what export, delete and provision want; `id` addresses the Agent on
the roster.

### Deploying replaces, never updates

Dify's importer only creates new Agent apps, so `deploy(agent, app_id=...)` is
refused before the request. Keep the id it returns.

## Exported DSL is safe to commit

`to_yaml()` blanks secret environment variables and anything credential-shaped,
matching the rule Dify's own portable-package builder applies. Model credentials
are an argument to the run and never serialised. Pass `include_secret=True` only
for output going somewhere as guarded as the secrets themselves.
