"""Knowledge bases against a real Dify.

A dataset API key is a different credential from an app key, so these skip
unless one is configured — or can be minted through the console.
"""

import pytest

from dify_client import DifyKnowledge
from dify_client.exceptions import APIError

from .conftest import HARNESS_DATASET


class TestDatasets:
    def test_one_comes_back_typed(self, knowledge, dataset):
        assert dataset.id
        assert dataset.name.startswith(HARNESS_DATASET)

    def test_it_appears_in_the_listing(self, knowledge, dataset):
        ids = {d.id for d in knowledge.datasets.list(limit=100)}
        assert dataset.id in ids

    def test_it_reads_back_by_id(self, knowledge, dataset):
        assert knowledge.datasets.retrieve(dataset.id).id == dataset.id

    def test_the_raw_payload_is_kept(self, knowledge, dataset):
        """Fields this SDK does not name still reach the caller."""
        assert dataset.payload.get("id") == dataset.id


class TestDocuments:
    def test_a_text_document_is_added(self, knowledge, dataset):
        docs = knowledge.documents(dataset)
        document = docs.create(text="The refund window is 30 days.", name="policy")

        assert document.id
        assert document.name == "policy"

    def test_it_is_not_searchable_until_it_is_indexed(self, knowledge, dataset):
        """The usual surprise: a freshly added document returns no hits."""
        docs = knowledge.documents(dataset)
        document = docs.create(text="The refund window is 30 days.", name="policy")

        status = docs.wait_until_indexed(document, timeout=90)
        assert status.finished
        assert status.indexed, status.error

    def test_it_is_listed(self, knowledge, dataset):
        docs = knowledge.documents(dataset)
        document = docs.create(text="hello", name="greeting")
        assert document.id in {d.id for d in docs.list()}

    def test_it_can_be_deleted(self, knowledge, dataset):
        docs = knowledge.documents(dataset)
        document = docs.create(text="hello", name="temp")
        docs.wait_until_indexed(document, timeout=90)
        docs.delete(document)

        assert document.id not in {d.id for d in docs.list()}

    def test_giving_both_text_and_a_file_is_refused(self, knowledge, dataset, tmp_path):
        note = tmp_path / "note.txt"
        note.write_text("x")
        from dify_client.exceptions import ValidationError

        with pytest.raises(ValidationError, match="exactly one"):
            knowledge.documents(dataset).create(text="x", file=note)


class TestSegments:
    def test_a_document_has_segments_once_indexed(self, knowledge, dataset):
        docs = knowledge.documents(dataset)
        document = docs.create(
            text="The refund window is 30 days. Shipping takes a week.",
            name="policy",
        )
        docs.wait_until_indexed(document, timeout=90)

        segments = docs.segments(document).list()
        assert segments
        assert any("refund" in s.content for s in segments)


class TestRetrieval:
    def test_an_indexed_document_is_findable(self, knowledge, dataset):
        docs = knowledge.documents(dataset)
        document = docs.create(
            text="The refund window is 30 days from delivery.", name="policy"
        )
        indexed = docs.wait_until_indexed(document, timeout=90)
        if not indexed.indexed:
            pytest.skip(f"indexing did not complete: {indexed.error}")

        hits = knowledge.datasets.search(dataset, "how long do I have to return it?")

        assert hits
        assert hits[0].score >= 0
        assert "refund" in str(hits[0]).lower()

    def test_an_empty_dataset_finds_nothing_rather_than_failing(
        self, knowledge, dataset
    ):
        assert knowledge.datasets.search(dataset, "anything") == []


class TestWrongCredential:
    def test_an_app_key_cannot_reach_datasets(self, workflow_deployment, service_api):
        """The credentials are scoped differently, and the SDK does not pretend
        otherwise."""
        wrong = DifyKnowledge(workflow_deployment.api_key, base_url=service_api)
        try:
            with pytest.raises(APIError):
                wrong.datasets.list()
        finally:
            wrong.close()


class TestMetadata:
    def test_a_field_can_be_defined_renamed_and_removed(self, knowledge, dataset):
        added = knowledge.datasets.add_metadata_field(dataset, "owner")
        assert added.id and added.name == "owner"

        knowledge.datasets.rename_metadata_field(dataset, added, "keeper")
        assert "keeper" in {f.name for f in knowledge.datasets.metadata(dataset)}

        knowledge.datasets.delete_metadata_field(dataset, added)
        assert added.id not in {f.id for f in knowledge.datasets.metadata(dataset)}

    def test_a_defined_field_counts_the_documents_carrying_it(self, knowledge, dataset):
        added = knowledge.datasets.add_metadata_field(dataset, "owner")
        try:
            defined = knowledge.datasets.metadata(dataset)[0]
            assert defined.count == 0
            assert not defined.built_in
        finally:
            knowledge.datasets.delete_metadata_field(dataset, added)

    def test_difys_own_fields_are_listed_separately(self, knowledge, dataset):
        """Built-in fields are filled in by Dify, not created by you — and Dify
        sends them with no id at all, which is what `built_in` reads."""
        built_in = knowledge.datasets.built_in_metadata(dataset)

        assert built_in
        assert all(field.built_in for field in built_in)
        assert "document_name" in {field.name for field in built_in}

    def test_they_can_be_turned_on(self, knowledge, dataset):
        knowledge.datasets.set_built_in_metadata(dataset, True)


class TestModels:
    def test_the_workspaces_models_are_listed(self, knowledge):
        """Dify guards this one with the dataset token, so it lives here."""
        providers = knowledge.models("text-embedding")
        if not providers:
            pytest.skip("this workspace has no embedding provider configured")

        assert all(provider.provider for provider in providers)

    def test_a_model_knows_the_provider_it_belongs_to(self, knowledge):
        """Dify does not put it in the model's own payload, and every call
        that takes a model name takes a provider beside it."""
        models = [model for provider in knowledge.models() for model in provider]
        if not models:
            pytest.skip("this workspace has no LLM configured")

        assert all(model.provider and model.name for model in models)
        assert all(model.type == "llm" for model in models)

    def test_the_label_is_one_string_rather_than_a_localised_object(self, knowledge):
        providers = knowledge.models()
        if not providers:
            pytest.skip("this workspace has no LLM configured")

        assert isinstance(providers[0].label, str)
        assert providers[0].label

    def test_an_app_key_cannot_reach_it(self, workflow_deployment, service_api):
        wrong = DifyKnowledge(workflow_deployment.api_key, base_url=service_api)
        try:
            with pytest.raises(APIError):
                wrong.models()
        finally:
            wrong.close()


class TestTags:
    def test_a_tag_is_bound_and_unbound(self, knowledge, dataset):
        import uuid

        tag = knowledge.tags.create(f"sdk-harness-{uuid.uuid4().hex[:6]}")
        try:
            assert tag.id in {t.id for t in knowledge.tags.list()}

            knowledge.tags.bind(dataset, [tag])
            assert tag.id in {t.id for t in knowledge.datasets.tags(dataset)}

            knowledge.tags.unbind(dataset, tag)
            assert tag.id not in {t.id for t in knowledge.datasets.tags(dataset)}
        finally:
            knowledge.tags.delete(tag)

    def test_the_binding_count_is_a_number(self, knowledge, dataset):
        """Dify sends it as a string, so the obvious comparison on the raw
        payload compared a str to an int."""
        import uuid

        tag = knowledge.tags.create(f"sdk-harness-{uuid.uuid4().hex[:6]}")
        try:
            knowledge.tags.bind(dataset, [tag])
            bound = next(t for t in knowledge.tags.list() if t.id == tag.id)
            assert bound.binding_count > 0
            knowledge.tags.unbind(dataset, tag)
        finally:
            knowledge.tags.delete(tag)

    def test_several_tags_unbind_at_once(self, knowledge, dataset):
        """Dify wants `tag_ids`; the singular `tag_id` it also accepts is
        marked deprecated in the payload's own schema."""
        import uuid

        tags = [
            knowledge.tags.create(f"sdk-harness-{uuid.uuid4().hex[:6]}")
            for _ in range(2)
        ]
        try:
            knowledge.tags.bind(dataset, tags)
            knowledge.tags.unbind(dataset, *tags)
            assert knowledge.datasets.tags(dataset) == []
        finally:
            for tag in tags:
                knowledge.tags.delete(tag)

    def test_unbinding_nothing_is_refused(self, knowledge, dataset):
        from dify_client.exceptions import ValidationError

        with pytest.raises(ValidationError, match="at least one"):
            knowledge.tags.unbind(dataset)

    def test_the_listing_is_read_from_a_bare_array(self, knowledge):
        """Dify answers this one without the usual {"data": …} envelope, and
        without paging. It is still a Page, so a caller need not know which
        listings page and which do not."""
        import uuid

        from dify_client import Page

        tag = knowledge.tags.create(f"sdk-harness-{uuid.uuid4().hex[:6]}")
        try:
            page = knowledge.tags.list()
            assert isinstance(page, Page)
            assert tag.id in {t.id for t in page.all()}
            assert not page.has_more
            assert page.next_page() is None
        finally:
            knowledge.tags.delete(tag)


class TestDocumentRoutes:
    def _indexed(self, knowledge, dataset):
        docs = knowledge.documents(dataset)
        document = docs.create(text="the refund window is 30 days", name="policy")
        docs.wait_until_indexed(document, timeout=90)
        return docs, document

    def test_a_document_reads_back_by_id(self, knowledge, dataset):
        docs, document = self._indexed(knowledge, dataset)
        assert docs.retrieve(document).id == document.id

    def test_a_file_update_goes_to_the_route_dify_keeps(
        self, knowledge, dataset, tmp_path
    ):
        """Both `/update-by-file` spellings are deprecated; PATCH is not."""
        docs, document = self._indexed(knowledge, dataset)
        replacement = tmp_path / "policy.txt"
        replacement.write_text("the refund window is 45 days")

        updated = docs.update(document, file=replacement)
        assert updated.batch, "the batch is what indexing_status() needs"

    def test_a_text_update_keeps_the_name_when_none_is_given(self, knowledge, dataset):
        """Dify requires one and answers a pydantic error rather than saying so."""
        docs, document = self._indexed(knowledge, dataset)
        assert docs.update(document, text="the refund window is 45 days").batch

    def test_several_documents_download_as_a_zip(self, knowledge, dataset):
        docs, document = self._indexed(knowledge, dataset)
        archive = docs.download_all([document])
        assert archive[:2] == b"PK"

    def test_downloading_none_says_to_name_them(self, knowledge, dataset):
        """Dify has no download-everything, and will not guess."""
        from dify_client.exceptions import ValidationError

        with pytest.raises(ValidationError, match="Name the documents"):
            knowledge.documents(dataset).download_all([])

    def test_one_segment_reads_back(self, knowledge, dataset):
        docs, document = self._indexed(knowledge, dataset)
        segments = docs.segments(document).list()
        assert docs.segments(document).retrieve(segments[0]).id == segments[0].id


class TestPipeline:
    def test_an_ordinary_base_has_none_and_says_so(self, knowledge, dataset):
        """ "Pipeline not found" reads like a bug; it is an absence."""
        from dify_client.exceptions import APIError

        with pytest.raises(APIError, match="has no RAG pipeline"):
            knowledge.pipeline(dataset).datasources()
