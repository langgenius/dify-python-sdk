# Testing a workflow

The point of the local path is that a CI job running workflow tests holds no
credential worth stealing and spends nothing.

## Stubbing the model

```python
from dify_client.workflow import StubLLM

result = wf.run(inputs, llm=StubLLM("a canned reply"))
assert result.node("prompt")["output"].startswith("Summarise")
assert not result.usage                      # nothing was spent
```

`StubLLM` also takes a callable receiving the prompt messages the workflow
actually built, which is what makes the *prompt* assertable rather than only the
answer:

```python
StubLLM(lambda messages: "SHORT" if "casual" in str(messages[0].content) else "LONG")
stub.calls          # every invocation's prompt messages, in order
```

Without a stub, an LLM node cannot be **built** — graphon resolves provider
credentials while constructing the node — so `run()` raises `WorkflowRunError`
rather than returning a failed result. That is why the stub is the default path,
not an optimisation.

## Code nodes

A code node posts its program to `dify-sandbox`, so on its own it fails locally.

```python
from dify_client.workflow import LocalSandbox, StubCode

wf.run(inputs, code=StubCode({"score": 0.9}))    # do not run it
wf.run(inputs, code=LocalSandbox())              # run it here, confined by the OS
```

`LocalSandbox` does not re-implement anything: graphon's template transformer
builds the program, so the source executed locally is byte-identical to what
Dify would have sent. Two things differ.

**The import surface.** Dify's sandbox sees its own interpreter and that
deployment's packages, **not** the project's virtualenv. A node allowed to reach
the virtualenv would `import pandas` happily in a test and fail in production, so
the default is the standard library alone:

```python
LocalSandbox()                       # stdlib only
LocalSandbox(packages=["httpx"])     # plus httpx and its dependency tree
LocalSandbox(packages=["httpx"]).importable()
```

The stock Dify sandbox image ships `httpx`; an operator can add more. To find
out what a given deployment has, deploy a code node that tries the imports in
question and read its output.

**The confinement.** Dify isolates with seccomp, which does not exist on macOS,
so `LocalSandbox` uses Seatbelt there and bubblewrap on Linux. Logic behaves the
same; a program that trips one confinement's limits will not necessarily trip
the other's. Network access is **off by default**, stricter than Dify's shipped
config, so a node making requests fails until `LocalSandbox(network=True)` says
otherwise — a visible failure rather than a test run quietly reaching the
internet.

For the exact confinement, point at a `dify-sandbox` already running:

```python
wf.run(inputs, credentials={"code": {
    "execution_endpoint": "http://localhost:8194",
    "execution_api_key": "dify-sandbox"}})
```

## What a run returns

`RunResult` has the same shape whether it came from a stub, a local run or a
real Dify, so assertions carry across unchanged:

```python
result.succeeded
result["answer"]                 # workflow-level outputs
result.node("prompt").outputs    # any node's inputs / outputs / status / error
result.nodes                     # only the nodes that ran
result.usage                     # tokens and cost, summed
result.stream                    # streamed chunks
```

`usage` is empty for a stubbed run, which makes *this test spent nothing* an
assertion rather than a belief. Costs are kept per currency in `usage.costs`;
`usage.total_price` raises rather than adding different currencies together.

## Billed tests

Running against a real instance costs money, so the path is gated rather than
merely available:

```python
from dify_client import DifyManagement
from dify_client.workflow import requires_live

@requires_live
def test_the_real_model_obeys_the_prompt():
    with DifyManagement().ephemeral_app(wf) as app:
        result = app.workflows.runs.create(INPUTS, max_tokens=2000)
    assert result.node("prompt")["output"].startswith("Summarise")
```

`ephemeral_app` creates the app, publishes it, mints its key and deletes it
afterwards — nothing to set up by hand, nothing left behind. It is deleted even
if the block raises; a hard crash can still leave one.

`@requires_live` skips unless `DIFY_LIVE_TESTS` is set **and** a credential
exists, and applies a `billed` marker so CI can drop these with
`pytest -m "not billed"`. Prefer `DIFY_LIVE_TESTS=1 pytest -m billed` over
exporting the flag, which stays on for every later run in that terminal.

Budgets are checked after the run — the charge is already made; what they stop
is the same overspend passing unnoticed every run after this one:

- `max_tokens` works wherever tokens are counted
- `max_cost` needs a reported price; if none was reported it is an error rather
  than a silent pass, because a priced check that cannot run is false assurance

Provider credentials are never read from the environment by `run()`. An ambient
default would quietly turn a test run into a billed call.
