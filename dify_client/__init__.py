"""A Python SDK for Dify.

Two things live here, with separate contracts:

**Using Dify.** Run an app, hold a conversation, search a knowledge base,
manage a workspace. The client holds the connection and the credential; what
you can do hangs off what it acts on::

    from dify_client import DifyApp

    app = DifyApp(api_key="app-…", user="alice")
    message = app.chat.messages.create("Hello")
    run = app.workflows.runs.create({"text": "…"})

**Defining Dify apps in code.** Build a workflow or an agent, test it without a
server, and deploy it. Needs ``pip install dify-client[workflow]``::

    from dify_client.workflow import Workflow, text_input

Three entry points, because Dify scopes three credentials:
:class:`DifyApp` (an app's key), :class:`DifyKnowledge` (a dataset key) and
:class:`DifyManagement` (a console session).
"""

from dify_client.agent import Agent, AgentError, dify_tool, portable_soul
from dify_client.app import (
    AppInfo,
    AppParameters,
    AsyncDifyApp,
    DifyApp,
    InputField,
    ServerInfo,
    SiteSettings,
)
from dify_client.catalog import Model, ModelProvider
from dify_client.compat import Capability, Compatibility, probe
from dify_client.console import (
    AgentSummary,
    ApiKey,
    App,
    DifyManagement,
    ImportResult,
    Trigger,
    WebhookTrigger,
)
from dify_client.exceptions import (
    APIError,
    AuthenticationError,
    DifyClientError,
    FileUploadError,
    NetworkError,
    RateLimitError,
    RequestTimeout,
    TransportError,
    ValidationError,
)
from dify_client.knowledge import AsyncDifyKnowledge, DifyKnowledge
from dify_client.lifecycle import Deployment, Stage
from dify_client.openapi import OpenApiClient, Workspace
from dify_client.resources import (
    Annotation,
    AnnotationReplyJob,
    Conversation,
    Dataset,
    Document,
    Form,
    IndexingStatus,
    MetadataField,
    PipelineIngestion,
    RetrievalHit,
    Segment,
    Tag,
    UploadedFile,
)
from dify_client.resources.management import ManagedApp
from dify_client.results import (
    MAX_WALK,
    AsyncPage,
    Message,
    NodeExecution,
    Page,
    PageLimitReached,
    WorkflowRun,
)
from dify_client.skills import Skill, SkillError, WorkspaceSkill
from dify_client.streams import (
    AsyncMessageStream,
    AsyncWorkflowRunStream,
    MessageStream,
    RunEvent,
    WorkflowRunStream,
)
from dify_client.tools import ToolCatalog, ToolParameter, ToolProvider, ToolSpec
from dify_client.usage import Usage
from dify_client.version import __version__

__all__ = [
    "__version__",
    # -- entry points ------------------------------------------------------
    "AsyncDifyApp",
    "AsyncDifyKnowledge",
    "DifyApp",
    "DifyKnowledge",
    "DifyManagement",
    "OpenApiClient",
    # -- what calls return -------------------------------------------------
    "Annotation",
    "AnnotationReplyJob",
    "App",
    "AppInfo",
    "AppParameters",
    "InputField",
    "SiteSettings",
    "Model",
    "ModelProvider",
    "MetadataField",
    "Tag",
    "Capability",
    "Compatibility",
    "probe",
    "ApiKey",
    "Conversation",
    "Dataset",
    "Deployment",
    "ManagedApp",
    "Stage",
    "Document",
    "Form",
    "ImportResult",
    "IndexingStatus",
    "Message",
    "NodeExecution",
    "Page",
    "AsyncPage",
    "PageLimitReached",
    "MAX_WALK",
    "PipelineIngestion",
    "RetrievalHit",
    "ServerInfo",
    "Segment",
    "Trigger",
    "UploadedFile",
    "Usage",
    "WebhookTrigger",
    "WorkflowRun",
    "Workspace",
    # -- streaming ---------------------------------------------------------
    "AsyncMessageStream",
    "AsyncWorkflowRunStream",
    "MessageStream",
    "RunEvent",
    "WorkflowRunStream",
    # -- errors ------------------------------------------------------------
    "APIError",
    "AuthenticationError",
    "DifyClientError",
    "FileUploadError",
    "NetworkError",
    "RateLimitError",
    "RequestTimeout",
    "TransportError",
    "ValidationError",
    # -- defining apps in code --------------------------------------------
    "Agent",
    "AgentError",
    "AgentSummary",
    "Skill",
    "SkillError",
    "ToolCatalog",
    "ToolParameter",
    "ToolProvider",
    "ToolSpec",
    "WorkspaceSkill",
    "dify_tool",
    "portable_soul",
]
