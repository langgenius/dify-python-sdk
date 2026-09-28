"""Node data for the node types Dify implements and graphon does not.

graphon is the engine, not the server. A node whose work *is* the server — an
agent strategy loaded from a plugin, a form waiting on a person, retrieval
against an index, a trigger Dify subscribes to — lives in Dify's own
``core.workflow.nodes``, and its schema is not published as a package. These
modules restate those schemas, read off Dify's entities rather than its docs.

A workflow using one of them is written, exported and deployed like any other
and **cannot be run by** ``wf.run()``: there is no local implementation to
call, and the failure says so and names the type. The one exception is the
knowledge node, which has a local stand-in — see
:mod:`dify_client.workflow.local_knowledge`.
"""

from ._errors import NodeError
from .agents import AgentNodeData, DifyAgentNodeData, NodeInput
from .human_input import (
    FileInputConfig,
    FileListInputConfig,
    FormInputConfig,
    HumanInputNodeData,
    ParagraphInputConfig,
    SelectInputConfig,
    StringListSource,
    StringSource,
    UserActionConfig,
    action,
    form_file,
    form_files,
    form_paragraph,
    form_select,
)
from .knowledge import (
    DATASOURCE,
    DEFAULT_TOP_K,
    KNOWLEDGE_INDEX,
    KNOWLEDGE_RETRIEVAL,
    DatasourceNodeData,
    KeywordSetting,
    KnowledgeIndexNodeData,
    KnowledgeRetrievalNodeData,
    MetadataCondition,
    MetadataFilteringCondition,
    MultipleRetrievalConfig,
    RerankingModelConfig,
    SingleRetrievalConfig,
    VectorSetting,
    WeightedScoreConfig,
)
from .triggers import (
    FREQUENCIES,
    TRIGGER_PLUGIN,
    ScheduleTriggerData,
    TriggerEventNodeData,
    WebhookMethod,
    WebhookParameter,
    WebhookTriggerData,
    body_field,
    header,
    query_param,
)

__all__ = [
    "NodeError",
    "DATASOURCE",
    "DEFAULT_TOP_K",
    "FREQUENCIES",
    "KNOWLEDGE_INDEX",
    "KNOWLEDGE_RETRIEVAL",
    "TRIGGER_PLUGIN",
    "AgentNodeData",
    "DatasourceNodeData",
    "DifyAgentNodeData",
    "FileInputConfig",
    "FileListInputConfig",
    "FormInputConfig",
    "HumanInputNodeData",
    "KeywordSetting",
    "KnowledgeIndexNodeData",
    "KnowledgeRetrievalNodeData",
    "MetadataCondition",
    "MetadataFilteringCondition",
    "MultipleRetrievalConfig",
    "NodeInput",
    "ParagraphInputConfig",
    "RerankingModelConfig",
    "ScheduleTriggerData",
    "SelectInputConfig",
    "SingleRetrievalConfig",
    "StringListSource",
    "StringSource",
    "TriggerEventNodeData",
    "UserActionConfig",
    "VectorSetting",
    "WebhookMethod",
    "WebhookParameter",
    "WebhookTriggerData",
    "WeightedScoreConfig",
    "action",
    "body_field",
    "form_file",
    "form_files",
    "form_paragraph",
    "form_select",
    "header",
    "query_param",
]
