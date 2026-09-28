"""The error a node's own rules raise.

A node module knows what Dify accepts; it does not know whether it is being
added to a workflow or a pipeline. It raises this, and the document turns it
into the ``WorkflowError`` callers already catch, so the message travels and
the vocabulary stays the document's.
"""


class NodeError(ValueError):
    """Raised when a node is configured in a way Dify would reject."""
