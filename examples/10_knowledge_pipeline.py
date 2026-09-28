"""A knowledge pipeline: documents in, an indexed knowledge base out.

A pipeline is the other document this SDK writes. It is a workflow in every
way except its ends — it starts at a datasource instead of a start node and
finishes at a knowledge base instead of an end node — and Dify serves it from
``/rag/pipelines`` as ``kind: rag_pipeline`` rather than ``kind: app``.

Three things this example is here to show:

- **How the base will be searched is decided here.** ``knowledge_index``
  writes the embedding model, the search method and the rerank model onto the
  knowledge base it creates, and every later retrieval reads them — including
  the one a workflow's knowledge node does.
- **The inputs a pipeline asks for live under ``rag``**, keyed by the
  datasource that wants them, so a reference to one has three parts.
- **A pipeline cannot run locally.** Both of its ends are the server, so
  ``pipe.run()`` refuses rather than pretending.

**The chunker is not optional.** A knowledge-index node takes structured
chunks, so wiring an extractor straight into it queues the document and then
fails *indexing*, after the run has already reported success. The chain below
is the one Dify's own templates use, and ``chunked_text`` builds its middle.
Both tools are marketplace plugins (``langgenius/dify_extractor`` and
``langgenius/general_chunker``): the document imports and publishes without
them, and the run is what fails, so install them before pointing this at real
files.

**The live half costs nothing to run** — nothing is indexed and no model is
called — but it does create a knowledge base in your workspace:

    export DIFY_LIVE_TESTS=1
    export DIFY_CONSOLE_TOKEN=ey…            # from the Dify console session
    export DIFY_HOST=http://localhost        # or your own host
    python examples/10_knowledge_pipeline.py

Without those it builds the pipeline, prints what it would deploy, and exits.
"""

import sys

from dify_client import DifyManagement
from dify_client.workflow import Pipeline, why_not_live
from dify_client.workflow.recipes import chunked_text

#: Read these off the workspace rather than typing them:
#: `DifyKnowledge.models("text-embedding")` and `console.models.names("rerank")`.
EMBEDDING = "langgenius/openai/openai:text-embedding-3-small"
RERANK = "langgenius/cohere/cohere:rerank-v3.5"


def build() -> Pipeline:
    """A pipeline that takes uploaded files and indexes what they say."""
    pipe = Pipeline(
        "sdk-example-handbook", description="Index the handbook for retrieval."
    )

    files = pipe.datasource(
        plugin_id="langgenius/file",
        provider="file",
        name="upload-file",
        title="File",
        id="files",
    )
    # An input the pipeline asks for before it runs. It belongs to the
    # datasource, and reads as {{#rag.files.source#}} — three parts, not two.
    pipe.variable(files, "source", label="Where this came from", required=False)

    # Read the file, then cut it into the chunks a knowledge base indexes.
    # `file_pipeline(...)` is this whole function in one call; the long form
    # is here because the middle is the part worth seeing.
    chunks = chunked_text(pipe, files["file"])

    pipe.knowledge_index(
        chunks["result"],
        indexing="high_quality",
        embedding=EMBEDDING,
        search="hybrid_search",
        rerank=RERANK,
        top_k=5,
        title="Knowledge Base",
        id="base",
    )

    # No connect() at all: every edge here is implied by a reference. Wiring
    # `files -> chunks` by hand would have *suppressed* the extractor's edge,
    # because a node wired by hand is left alone — the chunker would then run
    # straight from the datasource with nothing extracted.
    return pipe


def main() -> int:
    pipe = build()
    settings = pipe.nodes[-1].data.retrieval_model
    print(f"pipeline built: {len(pipe.nodes)} nodes")
    print(f"  inputs   : {[v.name for v in pipe.variables]}")
    print(f"  searched : {settings['search_method']}, top {settings['top_k']}")
    print(f"  reranked : {settings['reranking_model']['reranking_model_name']}")

    # Both ends are the server, so there is nothing to run here. The failure
    # says so rather than half-running the middle.
    try:
        pipe.run({})
    except Exception as refusal:  # noqa: BLE001 - the refusal is the point
        print(f"\nlocal run refused, as it should: {refusal}")

    blocked = why_not_live()
    if blocked:
        print(f"\nstopping before touching Dify: {blocked}")
        return 0

    console = DifyManagement()
    result = console.pipelines.deploy(pipe)
    print(f"\ndeployed — stage: {result.stage}")
    # Two ids, because they address two things: the pipeline holds the graph,
    # the knowledge base holds the documents.
    print(f"  pipeline : {result.pipeline_id}")
    print(f"  base     : {result.dataset_id}")

    # Dify names the base after the pipeline *plus a number*, always, so find
    # it by id rather than by name.
    (listed,) = [p for p in console.pipelines.list() if p.id == result.pipeline_id]
    print(f"  named    : {listed.name!r}")

    # Deleting the knowledge base is what deletes the pipeline.
    console.pipelines.delete(result)
    print("\ndeleted the knowledge base, and the pipeline with it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
