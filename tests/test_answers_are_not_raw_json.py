"""The answers a caller has to read fields out of are typed.

Not everything: an app's `meta`, a run log, a datasource plugin's descriptor
are open-ended shapes that a newer Dify extends, and naming their fields would
be a promise this SDK cannot keep. What is typed here is what a caller must
read to make the *next* call — the inputs a run takes, the model to configure a
knowledge base with, the tag to bind — where reading a raw payload means
knowing something Dify never wrote down.
"""

import httpx
import pytest

from dify_client import AppParameters, DifyKnowledge, Page, SiteSettings
from dify_client.app import _app_parameters, _site
from dify_client.catalog import providers_from

from .test_resource_api import Dify

# Copied from a real Dify 1.17.1 answer, trimmed.
PARAMETERS = {
    "opening_statement": None,
    "suggested_questions": [],
    "suggested_questions_after_answer": {"enabled": False},
    "speech_to_text": {"enabled": True},
    "text_to_speech": {"enabled": False, "voice": "", "language": ""},
    "retriever_resource": {"enabled": False},
    "annotation_reply": {"enabled": False},
    "more_like_this": {"enabled": False},
    "user_input_form": [
        {
            "text-input": {
                "variable": "word",
                "label": "Word of the day",
                "description": "",
                "type": "text-input",
                "required": True,
                "hide": False,
                "max_length": 48,
                "options": [],
            }
        },
        {
            "select": {
                "variable": "tone",
                "label": "Tone",
                "type": "select",
                "required": False,
                "hide": False,
                "default": "short",
                "options": ["short", "long"],
            }
        },
    ],
    "sensitive_word_avoidance": {"enabled": False},
    "file_upload": {},
    "system_parameters": {"file_size_limit": 15, "workflow_file_upload_limit": 10},
}

SITE = {
    "title": "Handbook",
    "chat_color_theme": None,
    "chat_color_theme_inverted": False,
    "icon_type": "emoji",
    "icon": "🤖",
    "icon_background": "#FFEAD5",
    "description": None,
    "copyright": None,
    "privacy_policy": None,
    "input_placeholder": None,
    "custom_disclaimer": "",
    "default_language": "en-US",
    "show_workflow_steps": True,
    "use_icon_as_answer_icon": False,
    "icon_url": None,
}

MODELS = [
    {
        "provider": "langgenius/openai/openai",
        "label": {"zh_Hans": "OpenAI", "en_US": "OpenAI"},
        "status": "active",
        "models": [
            {
                "model": "text-embedding-3-large",
                "label": {"en_US": "text-embedding-3-large"},
                "model_type": "text-embedding",
                "model_properties": {"context_size": 8191, "max_chunks": 32},
                "deprecated": False,
                "status": "active",
            },
            {
                "model": "text-embedding-ada-002",
                "label": {"zh_Hans": "旧モデル"},
                "model_type": "text-embedding",
                "deprecated": True,
                "status": "active",
            },
        ],
    }
]


class TestWhatAnAppTakes:
    """`user_input_form` arrives as a list of single-key objects. Unwrapping it
    to find out whether a field is required is not a caller's job."""

    def _parameters(self):
        return _app_parameters(PARAMETERS)

    def test_the_fields_are_named_by_what_you_pass_in_inputs(self):
        """`variable`, not `label`. They are often the same string, which is
        why using the label works until someone renames one."""
        assert [field.name for field in self._parameters()] == ["word", "tone"]
        assert self._parameters()["word"].label == "Word of the day"

    def test_a_field_is_found_by_that_name(self):
        assert self._parameters()["tone"].type == "select"

    def test_asking_for_one_that_is_not_declared_says_what_is(self):
        with pytest.raises(KeyError, match="word, tone"):
            self._parameters()["topic"]

    def test_required_is_the_subset_a_run_is_rejected_without(self):
        assert [field.name for field in self._parameters().required] == ["word"]

    def test_a_selects_options_come_through(self):
        assert self._parameters()["tone"].options == ("short", "long")

    def test_a_default_comes_through(self):
        assert self._parameters()["tone"].default == "short"

    def test_a_length_limit_comes_through(self):
        """Dify enforces this one; the SDK used to invent limits of its own."""
        assert self._parameters()["word"].max_length == 48
        assert self._parameters()["tone"].max_length is None

    def test_the_feature_toggles_are_flattened(self):
        """Each arrives as `{"enabled": bool}`."""
        features = self._parameters().features
        assert features["speech_to_text"] is True
        assert features["more_like_this"] is False

    def test_the_deployments_limits_are_kept_as_sent(self):
        assert self._parameters().system_parameters["file_size_limit"] == 15

    def test_nothing_is_lost(self):
        assert self._parameters().payload == PARAMETERS

    def test_an_entry_dify_has_no_name_for_is_skipped(self):
        """Rather than becoming a field called ""."""
        odd = {**PARAMETERS, "user_input_form": [{"text-input": {"label": "x"}}, {}]}
        assert len(_app_parameters(odd)) == 0

    def test_an_app_with_no_inputs_is_empty_rather_than_absent(self):
        assert list(_app_parameters({})) == []


class TestWhatAVisitorSees:
    def test_the_settings_are_typed(self):
        site = _site(SITE)
        assert isinstance(site, SiteSettings)
        assert site.title == "Handbook"
        assert site.show_workflow_steps is True

    def test_nulls_read_as_empty_rather_than_none(self):
        """Dify sends null for every unset string here, and `site.description
        or ""` at each call site is noise."""
        assert _site(SITE).description == ""
        assert _site(SITE).chat_color_theme == ""

    def test_nothing_is_lost(self):
        assert _site(SITE).payload == SITE


class TestWhatTheWorkspaceCanCall:
    def test_a_model_carries_the_provider_it_was_listed_under(self):
        """Dify does not put it in the model's payload, and every call that
        takes a model name takes a provider beside it."""
        model = providers_from(MODELS)[0].models[0]
        assert model.name == "text-embedding-3-large"
        assert model.provider == "langgenius/openai/openai"

    def test_a_provider_iterates_its_models(self):
        assert [model.name for model in providers_from(MODELS)[0]] == [
            "text-embedding-3-large",
            "text-embedding-ada-002",
        ]

    def test_a_localised_label_becomes_one_string(self):
        assert providers_from(MODELS)[0].label == "OpenAI"

    def test_a_label_in_another_language_is_used_rather_than_dropped(self):
        assert providers_from(MODELS)[0].models[1].label == "旧モデル"

    def test_deprecated_is_not_usable_even_when_active(self):
        active, deprecated = providers_from(MODELS)[0].models
        assert active.usable
        assert not deprecated.usable

    def test_the_properties_come_through(self):
        assert providers_from(MODELS)[0].models[0].properties["context_size"] == 8191

    def test_an_empty_answer_is_an_empty_list(self):
        assert providers_from(None) == []


class TestTagsAndMetadata:
    def _knowledge(self, dify):
        return DifyKnowledge(
            "dataset-key",
            http_client=httpx.Client(
                transport=httpx.MockTransport(dify.handler),
                base_url="https://dify.test/v1",
            ),
        )

    def test_a_binding_count_is_a_number(self):
        """Dify sends it as a string."""
        dify = Dify(
            **{
                "/datasets/tags": httpx.Response(
                    200,
                    json=[
                        {
                            "id": "t",
                            "name": "x",
                            "type": "knowledge",
                            "binding_count": "3",
                        }
                    ],
                )
            }
        )
        with self._knowledge(dify) as knowledge:
            page = knowledge.tags.list()

        assert isinstance(page, Page)
        assert page[0].binding_count == 3

    def test_a_tag_can_be_passed_back_where_an_id_is_wanted(self):
        dify = Dify(**{"/datasets/tags/binding": httpx.Response(200, json={})})
        from dify_client import Tag

        with self._knowledge(dify) as knowledge:
            knowledge.tags.bind("ds-1", [Tag(id="t-1", name="x")])

        assert dify.calls[0]["body"]["tag_ids"] == ["t-1"]

    def test_a_field_with_no_id_is_one_of_difys_own(self):
        dify = Dify(
            **{
                "/datasets/ds-1/metadata/built-in": httpx.Response(
                    200, json={"fields": [{"name": "document_name", "type": "string"}]}
                )
            }
        )
        with self._knowledge(dify) as knowledge:
            fields = knowledge.datasets.built_in_metadata("ds-1")

        assert fields[0].built_in
        assert fields[0].name == "document_name"

    def test_a_field_you_defined_is_not(self):
        dify = Dify(
            **{
                "/datasets/ds-1/metadata": httpx.Response(
                    200,
                    json={
                        "doc_metadata": [
                            {"id": "f-1", "name": "owner", "type": "string", "count": 2}
                        ]
                    },
                )
            }
        )
        with self._knowledge(dify) as knowledge:
            field = knowledge.datasets.metadata("ds-1")[0]

        assert not field.built_in
        assert field.count == 2

    def test_a_field_can_be_passed_back_where_an_id_is_wanted(self):
        dify = Dify(**{"/datasets/ds-1/metadata/f-1": httpx.Response(200, json={})})
        from dify_client import MetadataField

        with self._knowledge(dify) as knowledge:
            knowledge.datasets.delete_metadata_field(
                "ds-1", MetadataField(id="f-1", name="owner")
            )

        assert dify.calls[0]["path"] == "/datasets/ds-1/metadata/f-1"


class TestWhatIsDeliberatelyStillADict:
    """Typing these would be a promise about shapes Dify keeps extending."""

    def test_the_app_meta_is_a_dict(self):
        from dify_client import DifyApp

        assert DifyApp.meta.__annotations__["return"] == "dict[str, Any]"

    def test_a_run_log_is_a_dict(self):
        from dify_client.resources.runs import WorkflowRuns

        assert "dict[str, Any]" in WorkflowRuns.logs.__annotations__["return"]

    def test_parameters_still_carries_everything_it_was_sent(self):
        """The escape hatch: a field this SDK does not name is on `payload`."""
        assert AppParameters(payload={"future": 1}).payload["future"] == 1
