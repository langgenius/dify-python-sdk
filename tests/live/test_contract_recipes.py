"""The shipped shapes, against a real Dify.

A recipe is a promise that this shape works. The only way to keep it is to
deploy each one to a server, so these import and publish every recipe and
delete what they made. Nothing calls a model, so the suite stays free.
"""

import uuid

import pytest
import yaml

from dify_client.workflow import Workflow, text_input
from dify_client.workflow.recipes import (
    approval,
    extract_fields,
    grounded_answer,
    rag_answer,
)

from .conftest import HARNESS_PREFIX


def _named(what: str) -> str:
    return f"{HARNESS_PREFIX}-{what}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def llm_model(management):
    names = management.models.names("llm")
    if not names:
        pytest.skip("no llm provider is configured on this Dify")
    return names[0]


def _deployed(management, wf: Workflow):
    result = management.apps.deploy(wf)
    result.raise_for_stage()
    return result, yaml.safe_load(management.apps.export(result.app_id))


class TestRagAnswer:
    def test_dify_takes_it_as_it_is(self, management, dataset, llm_model):
        wf = rag_answer(dataset=dataset.id, model=llm_model, name=_named("rag"))

        result, document = _deployed(management, wf)
        try:
            types = [
                node["data"]["type"] for node in document["workflow"]["graph"]["nodes"]
            ]
            assert types == ["start", "knowledge-retrieval", "llm", "answer"]
            # The edges nobody wrote are the edges Dify was given.
            assert len(document["workflow"]["graph"]["edges"]) == len(wf.edges)
        finally:
            management.apps.delete(result.app_id)


class TestFragments:
    def test_an_extractor_and_a_gate_deploy_together(self, management, llm_model):
        """Two fragments in one workflow, wired only where control flow needs it."""
        wf = Workflow(_named("fragments"))
        start = wf.start([text_input("email")])
        fields = extract_fields(
            wf,
            start["email"],
            {"order_id": "the order number"},
            model=llm_model,
            required=["order_id"],
        )
        gate = approval(wf, "Refund this order?\n\n{{#$output.note#}}")
        refund = wf.template("refunding {{ id }}", variables={"id": fields["order_id"]})
        decline = wf.template("declined", id="decline")
        wf.connect(fields, gate)
        wf.connect(gate.case("approve"), refund)
        wf.connect(gate.case("reject"), decline)
        wf.connect(gate.timeout, decline)
        wf.end({"result": wf.merge(refund, decline).output})

        result, document = _deployed(management, wf)
        try:
            handles = {
                edge["sourceHandle"] for edge in document["workflow"]["graph"]["edges"]
            }
            assert {"approve", "reject", "__timeout"} <= handles
        finally:
            management.apps.delete(result.app_id)

    def test_a_grounded_answer_can_be_part_of_a_larger_workflow(
        self, management, dataset, llm_model
    ):
        wf = Workflow(_named("grounded"))
        start = wf.start([text_input("question")])
        reply = grounded_answer(
            wf, start["question"], dataset=dataset.id, model=llm_model
        )
        wf.end({"answer": reply.output})

        result, _ = _deployed(management, wf)
        management.apps.delete(result.app_id)
