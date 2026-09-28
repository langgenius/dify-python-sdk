"""Workflow shapes that are known to work, as functions you can start from.

Most Dify apps are one of a handful of shapes: answer from a knowledge base,
pull fields out of a message, stop for a person to approve something. Building
one from nodes is not hard, but it is the same work every time, and the parts
that are easy to get wrong — a grounding prompt that lets the model answer
from memory, a branch with no aggregator, a form with no timeout arm — are the
parts nobody thinks about twice.

Each recipe here is built from the ordinary helpers and covered by a test that
deploys it to a real Dify, so it is a starting point that is known to import,
publish and run rather than a snippet from a README.

Two forms, and the difference matters:

* A **fragment** takes a workflow and a reference, adds nodes to it, and
  returns the node its output comes from. Fragments compose: the output of one
  is the input of the next, and everything around them is still yours.
* A **workflow** is a fragment with its ends attached, ready to deploy.

::

    from dify_client.workflow.recipes import grounded_answer, rag_answer

    wf = rag_answer(dataset=DATASET_ID, model=MODEL)     # the whole thing

    reply = grounded_answer(wf, start["question"],       # or a piece of one
                            dataset=DATASET_ID, model=MODEL)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .builder import Workflow
from .graph import GraphDocument
from .nodes.human_input import action, form_paragraph
from .nodes.models import parameter
from .nodes.tools import builtin_tool_data
from .parts import WorkflowError
from .pipeline import Pipeline
from .refs import Branch, Node, VarRef

__all__ = [
    "CHUNKER",
    "EXTRACTOR",
    "GROUNDING",
    "approval",
    "chunked_text",
    "file_pipeline",
    "extract_fields",
    "grounded_answer",
    "rag_answer",
]

#: The instruction that keeps an answer to what was retrieved.
#:
#: A model asked to "use the context" will still answer from memory when the
#: context is thin, and a workflow that reads well in testing then invents
#: policy in production. Saying what to do when the context does *not* cover
#: the question is the part that changes behaviour.
GROUNDING = (
    "Answer the question using only the context below. "
    "If the context does not contain the answer, say you do not know — "
    "do not use anything else you know.\n\n"
    "Context:\n{context}\n\nQuestion: {question}"
)


def _filled(instruction: str, *, context: VarRef, question: VarRef) -> str:
    """Put the two references into the prompt, leaving every other brace alone.

    This was ``str.format`` once, which meant an instruction that showed the
    model a JSON example raised ``KeyError`` on the example's own braces.
    """
    missing = [
        name for name in ("context", "question") if "{%s}" % name not in instruction
    ]
    if missing:
        listed = " and ".join(f"{{{name}}}" for name in missing)
        verb = "go" if len(missing) > 1 else "goes"
        msg = (
            f"This instruction never says where {listed} {verb}, so the answer "
            "would not be grounded in what was retrieved. Start from "
            "recipes.GROUNDING, which marks both."
        )
        raise WorkflowError(msg)
    return instruction.replace("{context}", context.template).replace(
        "{question}", question.template
    )


def grounded_answer(
    wf: GraphDocument,
    question: VarRef | Node,
    *,
    dataset: str | Sequence[str],
    model: str,
    top_k: int = 3,
    rerank: str | None = None,
    instruction: str = GROUNDING,
    prefix: str = "",
) -> Node:
    """Retrieve from a knowledge base, then answer from what came back.

    Returns the LLM node, so what to do with the answer is still the caller's:
    ``wf.answer(reply)`` for a chatflow, ``wf.end({"answer": reply})`` for a
    workflow, or another fragment.

    ``instruction`` is the prompt, with ``{context}`` and ``{question}``
    marking where the two go. Nothing else in it is touched — a brace in a
    JSON example is a brace — so it is a substitution rather than a format
    string.

    ``prefix`` names the nodes when a workflow holds more than one of these.
    """
    datasets = [dataset] if isinstance(dataset, str) else list(dataset)
    # Resolved once, so the retrieval query and the prompt ask the same thing
    # even when the caller passed a node whose default output later changes.
    asked = question if isinstance(question, VarRef) else question.output
    hits = wf.knowledge(
        asked,
        datasets,
        top_k=top_k,
        rerank=rerank,
        title="Knowledge",
        id=f"{prefix}hits" if prefix else None,
    )
    return wf.llm(
        _filled(instruction, context=hits.output, question=asked),
        model=model,
        title="Answer",
        id=f"{prefix}answer" if prefix else None,
    )


def rag_answer(
    *,
    dataset: str | Sequence[str],
    model: str,
    name: str = "rag-answer",
    question: str = "question",
    top_k: int = 3,
    rerank: str | None = None,
    instruction: str = GROUNDING,
) -> Workflow:
    """A chatflow that answers from a knowledge base and says when it cannot.

    The whole app: a question in, a grounded answer out::

        wf = rag_answer(dataset=DATASET_ID, model=MODEL)
        console.apps.deploy(wf)

    ``question`` is the name of the input variable, which is also what the
    Service API expects in ``inputs``.
    """
    from .inputs import paragraph

    wf = Workflow(name, description="Answer from a knowledge base.")
    start = wf.start([paragraph(question, label="Question")])
    reply = grounded_answer(
        wf,
        start[question],
        dataset=dataset,
        model=model,
        top_k=top_k,
        rerank=rerank,
        instruction=instruction,
    )
    wf.answer(reply)
    return wf


def extract_fields(
    wf: GraphDocument,
    text: VarRef | Node,
    fields: Mapping[str, str],
    *,
    model: str,
    instruction: str | None = None,
    required: Sequence[str] = (),
    prefix: str = "",
) -> Node:
    """Pull named fields out of free text, and return the node holding them.

    ``fields`` maps a name to the description the model actually follows —
    ``{"order_id": "the order number, digits only"}`` — and each becomes an
    output of the returned node alongside ``__is_success`` and ``__reason``,
    which is how a failed extraction is told from one that found nothing.
    """
    return wf.extract_parameters(
        text if isinstance(text, VarRef) else text.output,
        [
            parameter(name, description=description, required=name in set(required))
            for name, description in fields.items()
        ],
        model=model,
        instruction=instruction,
        title="Extract",
        id=f"{prefix}fields" if prefix else None,
    )


def approval(
    wf: GraphDocument,
    summary: str,
    *,
    note: str = "note",
    approve: str = "Approve",
    reject: str = "Reject",
    timeout: int = 1,
    timeout_unit: str = "day",
    prefix: str = "",
) -> Branch:
    """Stop for a person, and come back along the arm they chose.

    Returns the branch, whose arms are ``approve``, ``reject`` and the
    timeout — the third is Dify's, taken when nobody answers in time, and a
    workflow that leaves it unconnected simply stops there::

        gate = approval(wf, f"Ship this?\\n\\n{draft}")
        wf.connect(gate.case("approve"), ship)
        wf.connect(gate.case("reject"), revise)
        wf.connect(gate.timeout, revise)

    ``summary`` is the markdown the person reads; ``{{#$output.note#}}`` in it
    renders the free-text field they can fill in.
    """
    return wf.human_input(
        summary,
        inputs=[form_paragraph(note)],
        actions=[
            action("approve", approve, style="primary"),
            action("reject", reject),
        ],
        timeout=timeout,
        timeout_unit=timeout_unit,
        title="Approval",
        id=f"{prefix}approval" if prefix else None,
    )


#: Dify's own extractor and chunker, the two tools every knowledge pipeline
#: needs between its datasource and its knowledge base. Both are marketplace
#: plugins: a document carrying them imports and publishes on a workspace that
#: does not have them, and fails when it runs.
EXTRACTOR = "langgenius/dify_extractor/dify_extractor"
CHUNKER = "langgenius/general_chunker/general_chunker"


def chunked_text(
    pipe: Pipeline,
    file: VarRef | Node,
    *,
    prefix: str = "",
) -> Node:
    """Read a file and cut it into the chunks a knowledge base indexes.

    This is the middle of every knowledge pipeline, and the part that cannot
    be worked out from the DSL: a knowledge-index node takes structured
    chunks, so wiring an extractor straight into it queues the document and
    then fails *indexing* — after the run reports success.

    Returns the chunker node; ``pipe.knowledge_index(chunks["result"])`` is
    what reads it.
    """
    source = file if isinstance(file, VarRef) else file.output
    extract = pipe.add(
        builtin_tool_data(
            provider=EXTRACTOR,
            tool="dify_extractor",
            label="Dify Extractor",
            parameters={"file": {"type": "variable", "value": source.selector}},
        ),
        id=f"{prefix}extract" if prefix else None,
    )
    return pipe.add(
        builtin_tool_data(
            provider=CHUNKER,
            tool="general_chunker",
            label="General Chunker",
            parameters={
                "input_variable": {
                    "type": "mixed",
                    "value": str(extract["output"]),
                }
            },
        ),
        id=f"{prefix}chunk" if prefix else None,
    )


def file_pipeline(
    *,
    name: str,
    embedding: str | None = None,
    rerank: str | None = None,
    indexing: str = "economy",
    structure: str = "text_model",
    search: str | None = None,
    top_k: int = 3,
) -> Pipeline:
    """A knowledge pipeline that takes uploaded files and indexes what they say.

    The whole chain, in the order Dify's own templates use: a file datasource,
    its extractor, a chunker, and the knowledge base the chunks land in::

        pipe = file_pipeline(name="handbook", indexing="high_quality",
                             embedding=EMBEDDING, rerank=RERANK)
        result = console.pipelines.deploy(pipe)

    Running it is the knowledge client's job — upload a file, then
    ``knowledge.pipeline(result.dataset_id).run(...)``.
    """
    pipe = Pipeline(name, description="Index uploaded files.")
    files = pipe.datasource(
        plugin_id="langgenius/file",
        provider="file",
        name="upload-file",
        title="File",
        id="files",
    )
    chunks = chunked_text(pipe, files["file"])
    pipe.knowledge_index(
        chunks["result"],
        structure=structure,
        indexing=indexing,
        embedding=embedding,
        search=search,
        rerank=rerank,
        top_k=top_k,
        id="base",
    )
    return pipe
