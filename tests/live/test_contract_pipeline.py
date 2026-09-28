"""Knowledge pipelines against a real Dify.

A pipeline is served from ``/rag/pipelines``, not ``/apps``, and importing one
creates a knowledge base. These tests build one in code, deploy it, read it
back and delete the base — which is what deletes the pipeline.

Nothing here indexes a document or calls a model, so the suite stays free.
"""

import uuid

import pytest
import yaml

from dify_client.workflow import Pipeline
from dify_client.workflow.nodes import DifyAgentNodeData

from .conftest import HARNESS_PREFIX


def _named() -> str:
    return f"{HARNESS_PREFIX}-pipeline-{uuid.uuid4().hex[:8]}"


def _file_pipeline(name: str) -> Pipeline:
    pipe = Pipeline(name, description="built by the SDK harness")
    files = pipe.datasource(
        plugin_id="langgenius/file", provider="file", name="upload-file", title="File"
    )
    index = pipe.knowledge_index(files["file"], title="Knowledge Base")
    pipe.connect(files, index)
    return pipe


@pytest.fixture
def deployed(management):
    """A published pipeline, deleted with its knowledge base afterwards."""
    result = management.pipelines.deploy(_file_pipeline(_named()))
    result.raise_for_stage()
    yield result
    management.pipelines.delete(result)


class TestDeployingAPipeline:
    def test_it_publishes_and_reports_both_ids(self, deployed):
        """The pipeline holds the graph, the dataset holds the documents."""
        assert deployed.published
        assert deployed.pipeline_id
        assert deployed.dataset_id
        assert deployed.pipeline_id != deployed.dataset_id

    def test_the_document_comes_back_as_a_pipeline(self, deployed, management):
        document = yaml.safe_load(management.pipelines.export(deployed))

        assert document["kind"] == "rag_pipeline"
        assert [
            node["data"]["type"] for node in document["workflow"]["graph"]["nodes"]
        ] == ["datasource", "knowledge-index"]

    def test_it_is_listed_through_the_knowledge_base_that_owns_it(
        self, deployed, management
    ):
        (listed,) = [
            p for p in management.pipelines.list() if p.id == deployed.pipeline_id
        ]

        assert listed.dataset_id == deployed.dataset_id
        assert listed.published

    def test_the_knowledge_base_is_named_after_the_pipeline_plus_a_number(
        self, deployed, management
    ):
        """Dify appends one unconditionally, so finding the base by name fails.

        `generate_incremental_name` returns "<name> 1" even when nothing
        collides, which is why the SDK hands back a dataset_id instead.
        """
        (listed,) = [
            p for p in management.pipelines.list() if p.id == deployed.pipeline_id
        ]

        assert listed.name.endswith(" 1")


class TestWhatAPipelineRefuses:
    def test_a_knowledge_base_without_search_settings_is_refused(self, management):
        """Dify validates the node as a KnowledgeConfiguration, which requires them.

        The builder always writes a default for exactly this reason; this test
        is what proves the requirement is the server's, not an invention.
        """
        pipe = _file_pipeline(_named())
        index = next(n for n in pipe.nodes if n.type == "knowledge-index")
        index.data.retrieval_model = None

        result = management.pipelines.import_definition(pipe.to_yaml())
        try:
            assert not result.imported
            assert "retrieval_model" in result.error
        finally:
            if result.dataset_id:
                management.pipelines.delete(result)


class TestWhatAPipelineSilentlyDrops:
    def test_an_agent_package_does_not_survive_a_pipeline_import(self, management):
        """Which is why the helpers that need one are on the app document.

        The pipeline importer never materialises an agent binding, but it does
        not refuse the document either: it imports, publishes, and comes back
        without the package the node's binding names.
        """
        from dify_client import Agent

        pipe = _file_pipeline(_named())
        source = pipe.nodes[0]
        agent = pipe.add(
            DifyAgentNodeData(
                title="Agent",
                agent_binding={
                    "binding_type": "inline_agent",
                    "package_ref": "agent_1",
                },
            )
        )
        index = next(n for n in pipe.nodes if n.type == "knowledge-index")
        pipe._edges.clear()
        pipe.connect(source, agent, index)

        document = pipe.to_dict()
        document["agent_packages"] = {
            "agent_1": Agent("inline", soul={"schema_version": 1}).to_package()
        }

        result = management.pipelines.deploy(
            yaml.safe_dump(document, allow_unicode=True)
        )
        try:
            result.raise_for_stage()
            back = yaml.safe_load(management.pipelines.export(result))
            assert not back.get("agent_packages")
        finally:
            if result.dataset_id:
                management.pipelines.delete(result)


class TestRunningOneBuiltFromCode:
    """The two halves meet here: the document this SDK writes, run by its client.

    Until a pipeline could be built from code, these routes could only be
    reached through a pipeline made by hand in the console — which is why the
    datasource listing was being read as an envelope it never had.
    """

    def test_the_datasource_node_comes_back_by_the_id_it_was_given(
        self, deployed, knowledge
    ):
        (source,) = knowledge.pipeline(deployed.dataset_id).datasources()

        assert source["node_id"] == "datasource"
        assert source["datasource_type"] == "local_file"

    def test_a_file_uploaded_for_it_keeps_its_extension(self, knowledge, tmp_path):
        """Dify chooses how to read a document by extension, and rejects none."""
        document = tmp_path / "refunds.txt"
        document.write_text("Refunds take 5 business days.", encoding="utf-8")

        uploaded = knowledge.upload_for_pipeline(document)

        assert uploaded["extension"] == "txt"
        assert uploaded["name"].endswith(".txt")

    def test_an_open_file_keeps_its_name_too(self, knowledge, tmp_path):
        """It went up as "document", and was refused for having no extension."""
        document = tmp_path / "refunds.txt"
        document.write_text("Refunds take 5 business days.", encoding="utf-8")

        with document.open("rb") as handle:
            uploaded = knowledge.upload_for_pipeline(handle)

        assert uploaded["name"] == "refunds.txt"
        assert uploaded["extension"] == "txt"
        assert uploaded["size"] == len("Refunds take 5 business days.")

    def test_running_it_queues_a_document_under_one_batch(
        self, deployed, knowledge, tmp_path
    ):
        """A published run enqueues rather than indexes, so it answers at once.

        What it does *not* do is index this document: a knowledge-index node
        takes structured chunks, and this pipeline has no chunker between its
        datasource and its base — that needs a plugin. The queueing is the part
        this SDK is responsible for.
        """
        document = tmp_path / "refunds.txt"
        document.write_text("Refunds take 5 business days.", encoding="utf-8")
        uploaded = knowledge.upload_for_pipeline(document)

        queued = knowledge.pipeline(deployed.dataset_id).run(
            start_node_id="datasource",
            datasource_type="local_file",
            datasource_info_list=[{"reference": uploaded["id"], "name": "refunds.txt"}],
            inputs={},
        )

        assert queued.batch
        assert [d.name for d in queued.documents] == ["refunds.txt"]
        assert all(d.batch == queued.batch for d in queued.documents)


class TestTheShippedChain:
    def test_dify_takes_the_recipe_as_it_is(self, management):
        """The chain a pipeline actually needs, deployed rather than described.

        Both middle tools are marketplace plugins. Dify resolves a tool when
        the node runs, so this imports and publishes on a workspace that does
        not have them — which is exactly why the shape has to be pinned here
        rather than left to a run that nobody can make free.
        """
        from dify_client.workflow.recipes import file_pipeline

        pipe = file_pipeline(name=_named())

        result = management.pipelines.deploy(pipe)
        try:
            result.raise_for_stage()
            document = yaml.safe_load(management.pipelines.export(result))
            assert [
                node["data"]["type"] for node in document["workflow"]["graph"]["nodes"]
            ] == ["datasource", "tool", "tool", "knowledge-index"]
            tools = [
                node
                for node in document["workflow"]["graph"]["nodes"]
                if node["data"]["type"] == "tool"
            ]
            assert (
                tools[1]["data"]["tool_parameters"]["input_variable"]["value"]
                == f"{{{{#{tools[0]['id']}.output#}}}}"
            )
        finally:
            if result.dataset_id:
                management.pipelines.delete(result)
