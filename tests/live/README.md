# The live harness

Contract tests against a running Dify. They exist because everything in this
SDK was verified by hand at least once, and doing that by hand again for every
Dify release does not scale.

## Running it

```bash
set -a && . ./.env && set +a      # or export DIFY_HOST, DIFY_CONSOLE_EMAIL, …

uv run pytest tests/live          # the harness
uv run pytest -m "not live"       # everything else, no server needed
uv run pytest                     # both; live skips itself when unconfigured
```

Without those variables the whole directory skips, so `pytest` on a laptop with
no Dify still passes. That is deliberate: a skipped harness is honest, a failing
one is noise.

## What it costs

Nothing, on purpose. The fixture apps are template nodes rather than model
nodes, so the harness exercises the SDK's contract with Dify without spending
on tokens. Tests that would call a model belong under the `billed` marker
instead — there is one, `test_billed_rerank.py`, which is the only way to tell
a rerank model that is *configured* on a knowledge base from one that is
actually *consulted*:

```bash
DIFY_LIVE_TESTS=1 uv run pytest tests/live/test_billed_rerank.py -m billed
```

It indexes sixteen short chunks, runs three workflows and makes one rerank
call. `DIFY_LIVE_TESTS` is the only thing it asks for beyond `.env`: the
harness logs in when the test runs, so no console token is needed. (The
SDK's own `requires_live` does want one, because a user's billed test has no
other way to reach Dify; the harness uses its own `billed` instead.) A skip
here reads `DIFY_LIVE_TESTS is not set` — and a skipped billed test proves
nothing.

## What it creates

A dataset API key, revoked on the way out — Dify caps a workspace at ten, and a
harness that mints one per run stops working on the eleventh with an
"Access token is invalid" that says nothing about the real cause. Listed keys
come back with their secrets masked, so reusing one is not an option.

Two apps per session — one `workflow`, one `advanced-chat` — plus whatever an
individual test needs, including knowledge pipelines, which are deleted through
the knowledge base that owns them. Everything is named `sdk-harness-…` and deleted at the
end, including anything a crashed earlier run left behind: the session sweeps
leftovers by prefix before it finishes.

## Absence is not failure

A Dify without triggers, or with `OPENAPI_ENABLED` off, is a supported Dify.
Tests ask before they assume:

```python
def test_webhooks(needs, management):
    needs("triggers")        # skips, naming the capability, if this Dify lacks it
```

`dify_client.compat.probe()` is what answers, and it is a library feature rather
than test scaffolding — the same pre-flight is useful before deploying a
workflow that depends on something the target server may not have. The run
prints what it found at the top:

```
Dify at http://localhost — 1.17.1 COMMUNITY
  SDK writes DSL 0.7.0
  agents: yes
  console_csrf: yes (this session carries one)
  human_input: yes
  openapi: yes
  triggers: yes
```

The version comes from `GET /v1/`, which needs no credential. Two servers on
the same version still disagree, because self-hosted deployments turn features
off individually — so the harness asks what works rather than inferring it from
the number.
