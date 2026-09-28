"""Operations grouped by what they act on.

A client holds the connection and the credential; a resource holds the verbs
for one kind of thing. Looking at ``app.workflows.runs`` shows everything you
can do to a run, which is the property worth having — the dots are only how it
is spelled.
"""

from ._base import Resource
from .annotations import Annotation, AnnotationReplyJob, Annotations, AsyncAnnotations
from .audio import AsyncAudio, Audio
from .completions import AsyncCompletions, Completions
from .conversations import AsyncConversations, Conversation, Conversations
from .files import AsyncFiles, Files, UploadedFile
from .forms import AsyncForms, Form, Forms
from .knowledge import (
    AsyncDatasets,
    AsyncDocuments,
    AsyncPipeline,
    AsyncSegments,
    AsyncTags,
    Dataset,
    Datasets,
    Document,
    Documents,
    IndexingStatus,
    MetadataField,
    Pipeline,
    PipelineIngestion,
    RetrievalHit,
    Segment,
    Segments,
    Tag,
    Tags,
)
from .messages import AsyncMessages, Messages
from .runs import AsyncWorkflowRuns, WorkflowRuns

__all__ = [
    "Annotation",
    "AnnotationReplyJob",
    "Annotations",
    "AsyncAnnotations",
    "AsyncAudio",
    "AsyncCompletions",
    "AsyncForms",
    "AsyncConversations",
    "AsyncDatasets",
    "AsyncDocuments",
    "AsyncPipeline",
    "AsyncSegments",
    "AsyncTags",
    "AsyncFiles",
    "AsyncMessages",
    "AsyncWorkflowRuns",
    "Audio",
    "Completions",
    "Conversation",
    "Conversations",
    "Dataset",
    "Datasets",
    "Document",
    "Documents",
    "Files",
    "Form",
    "Forms",
    "IndexingStatus",
    "MetadataField",
    "Messages",
    "Resource",
    "RetrievalHit",
    "Segment",
    "Pipeline",
    "PipelineIngestion",
    "Segments",
    "Tag",
    "Tags",
    "UploadedFile",
    "WorkflowRuns",
]
