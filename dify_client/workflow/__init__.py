"""Define, test and export Dify documents from Python.

This subpackage builds against ``graphon``, the engine Dify itself runs, so the
DSL it emits is checked against the server's own node schemas and the same
document can be executed locally without a Dify instance::

    from dify_client.workflow import Workflow, text_input

    wf = Workflow("greeter")
    start = wf.start([text_input("name")])
    greet = wf.template("Hello, {{ name }}!", variables={"name": start["name"]})
    answer = wf.answer(greet.output)
    wf.connect(start, greet, answer)

    wf.to_yaml("greeter.yml")
    assert wf.run({"name": "Dify"})["answer"] == "Hello, Dify!"

Two kinds of document come out of it, and they are not the same thing:
:class:`Workflow` is an app — a workflow or a chatflow — and :class:`Pipeline`
is a knowledge pipeline, which starts at a datasource and ends at a knowledge
base. The graph between those ends is identical, so both are built with the
same node helpers; what differs is the ends, the envelope and where Dify
serves them.

Not every node runs locally. graphon implements Dify's engine, not its server,
so retrieval, human input, agents, the document extractor and the triggers have
nothing to call here — ``run()`` says so and names the type. A knowledge node
is the one with a stand-in: ``run(inputs, knowledge=StubKnowledge([...]))``.

Install with ``pip install dify-client[workflow]``.
"""

from ..search import retrieval_model, weighted_score
from .builder import DSL_VERSION, ConversationVar, Workflow
from .conditions import OPERATORS, Condition, of_file, when
from .graph import GraphDocument, load_yaml
from .inputs import checkbox, file, file_list, number, paragraph, select, text_input
from .live import (
    BudgetExceeded,
    LiveRunError,
    live_enabled,
    requires_live,
    why_not_live,
)
from .local_knowledge import Chunk, StubKnowledge, knowledge_retriever
from .nodes import (
    AgentNodeData,
    DatasourceNodeData,
    DifyAgentNodeData,
    HumanInputNodeData,
    KnowledgeIndexNodeData,
    KnowledgeRetrievalNodeData,
    MetadataCondition,
    MetadataFilteringCondition,
    NodeError,
    ScheduleTriggerData,
    TriggerEventNodeData,
    WebhookParameter,
    WebhookTriggerData,
    action,
    body_field,
    form_file,
    form_files,
    form_paragraph,
    form_select,
    header,
    query_param,
)
from .nodes.agents import declared_output
from .nodes.http import api_key, basic, bearer
from .nodes.logic import loop_var
from .nodes.models import parameter
from .parts import Container, EnvVar, Iteration, Loop, WorkflowError
from .pipeline import PIPELINE_DSL_VERSION, Pipeline, PipelineVariable
from .recipes import (
    approval,
    chunked_text,
    extract_fields,
    file_pipeline,
    grounded_answer,
    rag_answer,
)
from .refs import Branch, Handle, Node, SystemVariables, VarRef, system
from .results import NodeResult, RunResult, WorkflowRunError
from .runner import run_dsl
from .sandbox import LocalSandbox, SandboxUnavailable, StubCode, code_executor
from .testing import StubLLM, run_with_stub, stub_models
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
    "GraphDocument",
    "weighted_score",
    "retrieval_model",
    "PIPELINE_DSL_VERSION",
    "PipelineVariable",
    "Pipeline",
    "KnowledgeIndexNodeData",
    "declared_output",
    "form_select",
    "form_paragraph",
    "form_files",
    "form_file",
    "action",
    "TriggerEventNodeData",
    "HumanInputNodeData",
    "DifyAgentNodeData",
    "DatasourceNodeData",
    "AgentNodeData",
    "loop_var",
    "Container",
    "Loop",
    "Iteration",
    "of_file",
    "when",
    "parameter",
    "bearer",
    "basic",
    "api_key",
    "OPERATORS",
    "Condition",
    "ConversationVar",
    "Branch",
    "Handle",
    "Node",
    "NodeResult",
    "RunResult",
    "SystemVariables",
    "Usage",
    "StubLLM",
    "StubKnowledge",
    "Chunk",
    "KnowledgeRetrievalNodeData",
    "MetadataCondition",
    "MetadataFilteringCondition",
    "knowledge_retriever",
    "VarRef",
    "Workflow",
    "NodeError",
    "WorkflowError",
    "WorkflowRunError",
    "approval",
    "extract_fields",
    "grounded_answer",
    "rag_answer",
    "chunked_text",
    "file_pipeline",
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
