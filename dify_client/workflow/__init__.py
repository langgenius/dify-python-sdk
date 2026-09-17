"""Define, test and export Dify workflows from Python.

This subpackage builds workflows against ``graphon``, the engine Dify itself
runs, so the DSL it emits is checked against the server's own node schemas and
the same document can be executed locally without a Dify instance::

    from dify_client.workflow import Workflow, text_input

    wf = Workflow("greeter")
    start = wf.start([text_input("name")])
    greet = wf.template("Hello, {{ name }}!", variables={"name": start["name"]})
    answer = wf.answer(greet.output)
    wf.connect(start, greet, answer)

    wf.to_yaml("greeter.yml")
    assert wf.run({"name": "Dify"})["answer"] == "Hello, Dify!"

Install with ``pip install dify-client[workflow]``.
"""

from .builder import DSL_VERSION, EnvVar, Workflow, WorkflowError, load_yaml
from .inputs import (
    checkbox,
    file,
    file_list,
    number,
    paragraph,
    select,
    text_input,
)
from .live import (
    BudgetExceeded,
    LiveRunError,
    live_enabled,
    requires_live,
    why_not_live,
)
from .refs import Node, SystemVariables, VarRef, system
from .results import NodeResult, RunResult, WorkflowRunError
from .runner import run_dsl
from .sandbox import LocalSandbox, SandboxUnavailable, StubCode, code_executor
from .testing import StubLLM, run_with_stub, stub_models
from .triggers import (
    ScheduleTriggerData,
    WebhookParameter,
    WebhookTriggerData,
    body_field,
    header,
    query_param,
)
from .usage import Usage

__all__ = [
    "DSL_VERSION",
    "ScheduleTriggerData",
    "WebhookParameter",
    "WebhookTriggerData",
    "body_field",
    "header",
    "query_param",
    "LocalSandbox",
    "SandboxUnavailable",
    "StubCode",
    "BudgetExceeded",
    "LiveRunError",
    "EnvVar",
    "Node",
    "NodeResult",
    "RunResult",
    "SystemVariables",
    "Usage",
    "StubLLM",
    "VarRef",
    "Workflow",
    "WorkflowError",
    "WorkflowRunError",
    "checkbox",
    "code_executor",
    "file",
    "file_list",
    "live_enabled",
    "load_yaml",
    "number",
    "paragraph",
    "run_dsl",
    "requires_live",
    "run_with_stub",
    "select",
    "stub_models",
    "system",
    "text_input",
    "why_not_live",
]
