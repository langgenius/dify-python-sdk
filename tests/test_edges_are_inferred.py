"""Edges the graph already implies, and the two rules that keep them honest.

A node that reads another node's output has said it runs after it. Writing
that twice — once as a reference and once as an edge — is what every early
workflow in this repo did, and forgetting the second half is what broke them.

Both rules here were bugs first: inferring into a branch arm made both arms
run, and inferring across a container boundary made a loop body depend on the
world outside it.
"""

from __future__ import annotations

import pytest

from dify_client.workflow import (
    StubCode,
    Workflow,
    WorkflowError,
    paragraph,
    system,
    text_input,
    when,
)


def _edges(wf: Workflow) -> set[tuple[str, str, str]]:
    return {(e.source, e.target, e.source_handle) for e in wf.edges}


def test_a_workflow_runs_with_no_connect_at_all():
    wf = Workflow("inferred")
    start = wf.start([text_input("q")])
    greet = wf.template("Hi {{ n }}", variables={"n": start["q"]})
    wf.answer(greet)

    assert wf.run({"q": "Dify"})["answer"] == "Hi Dify"


def test_a_template_reference_is_an_edge_too():
    """Both spellings count: the selector form and the {{#node.field#}} form."""
    wf = Workflow("inferred")
    start = wf.start([text_input("q")])
    wf.answer(f"you said {start['q']}")

    assert ("start", "answer", "source") in _edges(wf)


def test_a_namespace_is_not_a_node():
    """sys, env and conversation are not nodes, so they are not edges."""
    wf = Workflow("chat", mode="advanced-chat")
    start = wf.start([])
    token = wf.env_var("TOKEN", "x")
    greet = wf.template(
        "{{ q }} {{ t }}", variables={"q": system.query, "t": token}, id="greet"
    )
    wf.connect(start, greet)
    wf.answer(greet)

    assert {e.source for e in wf.edges} == {"start", "greet"}


def test_a_constant_list_that_starts_with_a_node_id_is_not_an_edge():
    """Plugin arguments are data even when one value happens to name a node."""
    wf = Workflow("constant")
    wf.start([text_input("q")])
    agent = wf.agent(
        "langgenius/agent/function_calling",
        {"choices": ["start", "literal"]},
    )
    wf.end({"answer": agent.output})

    assert ("start", "agent", "source") not in _edges(wf)


def test_an_arm_that_reads_from_before_the_branch_still_runs_alone():
    """The bug this rule exists for: both arms ran, because both read the query.

    An arm usually reads something from before the branch. That is a
    reference, not a second way in — inferring an edge there gives the arm a
    path that skips the branch entirely.
    """
    wf = Workflow("triage")
    start = wf.start([paragraph("message")])
    branch = wf.if_else([when(start["message"], "contains", "urgent")])
    hot = wf.template("PAGE — {{ m }}", variables={"m": start["message"]}, id="hot")
    cold = wf.template("Queued — {{ m }}", variables={"m": start["message"]}, id="cold")
    wf.connect(branch.true, hot)
    wf.connect(branch.false, cold)
    wf.answer(wf.merge(hot, cold))

    assert ("start", "hot", "source") not in _edges(wf)
    urgent = wf.run({"message": "urgent: look"}, raise_on_error=True)
    assert "hot" in urgent.nodes and "cold" not in urgent.nodes


def test_a_body_reading_the_world_outside_it_is_not_wired_to_it():
    """A loop body may read an outer variable; that is not a step in the body."""
    wf = Workflow("loop")
    start = wf.start([text_input("q")])
    with wf.loop(count=2) as body:
        wf.code(
            "def main(q): return {'n': 1}",
            variables={"q": start["q"]},
            outputs={"n": "number"},
            id="step",
        )
    end = wf.end({"o": start["q"]})
    wf.connect(start, body, end)

    assert ("start", "step", "source") not in _edges(wf)
    assert wf.run({"q": "x"}, code=StubCode({"n": 1})).status == "succeeded"


def test_what_was_written_by_hand_is_left_alone():
    wf = Workflow("mixed")
    start = wf.start([text_input("q")])
    one = wf.template("a {{ q }}", variables={"q": start["q"]}, id="one")
    two = wf.template("b {{ q }}", variables={"q": start["q"]}, id="two")
    wf.connect(one, two)
    wf.connect(start, one)
    wf.end({"o": two.output})

    # `two` was wired by hand, so nothing is added to it.
    assert ("start", "two", "source") not in _edges(wf)
    assert ("one", "two", "source") in _edges(wf)


def test_the_edges_a_document_will_carry_are_readable_before_it_is_sent():
    """Inference that cannot be inspected would be worse than the tedium."""
    wf = Workflow("inferred")
    start = wf.start([text_input("q")])
    wf.answer(wf.template("hi {{ q }}", variables={"q": start["q"]}, id="t"))

    assert _edges(wf) == {("start", "t", "source"), ("t", "answer", "source")}


def test_a_node_that_reads_nothing_is_still_reported():
    wf = Workflow("orphan")
    start = wf.start([text_input("q")])
    wf.template("nothing", id="lonely")
    wf.answer(start["q"])

    with pytest.raises(WorkflowError, match="not connected to anything"):
        wf.to_yaml()


class TestBranchHandles:
    def test_an_arm_names_itself_rather_than_being_typed(self):
        wf = Workflow("branch")
        start = wf.start([text_input("q")])
        branch = wf.if_else({"big": [when(start["q"], "is", "x")]})
        end = wf.end({"o": start["q"]})
        wf.connect(branch.case("big"), end)

        assert branch.handles == ("big", "false")
        assert ("if_else", "end", "big") in _edges(wf)

    def test_an_arm_it_does_not_have_is_refused(self):
        wf = Workflow("branch")
        start = wf.start([text_input("q")])
        branch = wf.if_else([when(start["q"], "is", "x")])

        with pytest.raises(KeyError, match="is not an arm"):
            branch.case("maybe")

    def test_a_classifier_and_a_form_name_their_arms_too(self):
        from dify_client.workflow import action

        wf = Workflow("arms")
        start = wf.start([text_input("q")])
        kind = wf.classify(
            start["q"],
            {"refund": "a refund", "other": "anything else"},
            model="langgenius/openai/openai:gpt-4o-mini",
        )
        review = wf.human_input("ok?", actions=[action("approve", "Approve")])

        assert kind.handles == ("refund", "other")
        assert review.handles == ("approve", "__timeout")


class TestAHandWrittenEdgeCanReplaceADerivedOne:
    """Wiring one node by hand switches inference off *for that node*, which
    is the rule — and it means an explicit edge can take the place of the one
    a reference needed rather than adding to it."""

    def test_connecting_past_a_node_is_refused(self):
        """The shape that shipped in an example: a chunker connected straight
        to its datasource reads an extractor nothing leads to, so the run
        chunks nothing and indexing fails long afterwards."""
        from dify_client.workflow import Pipeline
        from dify_client.workflow.recipes import chunked_text

        pipe = Pipeline("bypassed")
        files = pipe.datasource(
            plugin_id="langgenius/file", provider="file", id="files"
        )
        chunks = chunked_text(pipe, files["file"])
        index = pipe.knowledge_index(chunks["result"], id="base")
        pipe.connect(files, chunks, index)

        with pytest.raises(WorkflowError, match="read a node nothing leads to"):
            pipe.to_yaml()

    def test_a_container_reading_its_own_body_is_not_that(self):
        """`returns()` points the container at a node inside it, which runs
        within the container rather than before it."""
        wf = Workflow("iter")
        start = wf.start([text_input("q")])
        with wf.iteration(start["q"]) as each:
            greet = wf.template("hi {{ n }}", variables={"n": each.item})
            each.returns(greet.output)
        wf.end({"all": each.output})

        wf.to_yaml()  # does not raise

    def test_a_body_reading_the_world_outside_is_not_that_either(self):
        wf = Workflow("loop")
        start = wf.start([text_input("q")])
        with wf.loop(count=2) as body:
            wf.template("{{ q }}", variables={"q": start["q"]}, id="body")
        wf.connect(start, body)
        wf.end({"o": start["q"]})

        wf.to_yaml()  # does not raise


def test_a_node_is_indexed_by_name_not_by_position():
    """`Node.__getitem__` answered `node[0]`, so anything that iterated a node
    — `list(prompt)` in `wf.llm` — ran for ever building VarRefs."""
    wf = Workflow("scalar")
    start = wf.start([text_input("q")])
    greet = wf.template("hi", id="greet")

    with pytest.raises(TypeError, match="indexed by output name"):
        greet[0]
    with pytest.raises(TypeError):
        list(greet)
    assert start["q"].selector == ["start", "q"]


def test_a_node_is_a_prompt_in_its_own_right():
    wf = Workflow("scalar")
    start = wf.start([text_input("q")])
    greet = wf.template("hi {{ q }}", variables={"q": start["q"]}, id="greet")
    reply = wf.llm(greet, model="langgenius/openai/openai:gpt-4o-mini")

    assert reply.data.prompt_template[0].text == "{{#greet.output#}}"
    assert reply.data.prompt_template[0].role == "user"


class TestNothingIsInferredOutOfABranch:
    """An inferred edge was written with the handle ``source``, and a branch
    only follows edges named after one of its arms. A node that read a
    classifier's output with no ``connect()`` imported, published, and never
    ran — a local run reported ``succeeded`` with empty outputs."""

    def classified(self):
        wf = Workflow("routes")
        start = wf.start([text_input("q")])
        kind = wf.classify(
            start["q"],
            ["billing", "other"],
            model="langgenius/openai/openai:gpt-4o-mini",
            id="kind",
        )
        return wf, start, kind

    def test_no_edge_is_inferred_from_a_branch(self):
        wf, _, kind = self.classified()
        wf.template("{{ c }}", variables={"c": kind["class_name"]}, id="x")

        assert ("kind", "x") not in {(e.source, e.target) for e in wf.edges}

    def test_a_node_reading_a_branch_is_asked_which_arm_leads_to_it(self):
        wf, _, kind = self.classified()
        x = wf.template("{{ c }}", variables={"c": kind["class_name"]}, id="x")
        wf.end({"r": x.output})

        with pytest.raises(
            WorkflowError, match=r"wf.connect\(kind.case\(\.\.\.\), x\)"
        ):
            wf.to_dict()

    def test_naming_the_arm_is_enough(self):
        wf, _, kind = self.classified()
        x = wf.template("{{ c }}", variables={"c": kind["class_name"]}, id="x")
        wf.connect(kind.case("1"), x)
        wf.end({"r": x.output})

        wf.to_dict()
        assert ("kind", "x", "1") in _edges(wf)

    def test_connecting_out_of_a_branch_without_an_arm_is_refused(self):
        wf, start, kind = self.classified()

        with pytest.raises(WorkflowError, match="needs an arm"):
            wf.connect(kind, wf.end({"r": start["q"]}))

    def test_an_arm_spelled_as_a_string_is_checked(self):
        wf, start, kind = self.classified()

        with pytest.raises(WorkflowError, match="not an arm"):
            wf.connect(kind, wf.end({"r": start["q"]}), handle="billing")

    def test_a_node_reading_a_branch_and_something_before_it_still_waits(self):
        """Reading the start node as well gave it an inferred way in, so it
        ran in parallel with the classifier and read a class not yet chosen."""
        wf, start, kind = self.classified()
        x = wf.template(
            "{{ q }} {{ c }}",
            variables={"q": start["q"], "c": kind["class_name"]},
            id="x",
        )
        wf.end({"r": x.output})

        with pytest.raises(WorkflowError, match="which is a branch"):
            wf.to_dict()


class TestTheTimeoutArmIsSpelledTheWayDifySpellsIt:
    """Dify's engine and editor name it ``__timeout``. Writing ``timeout``
    produced an edge nothing takes, so a form nobody answered stopped the run
    instead of continuing where the caller connected it."""

    def form(self):
        from dify_client.workflow import action

        wf = Workflow("gate")
        start = wf.start([text_input("q")])
        review = wf.human_input("Ship?", actions=[action("ok", "OK")], id="review")
        wf.connect(start, review)
        return wf, start, review

    def test_the_arm_is_dify_s_name(self):
        _, _, review = self.form()

        assert review.timeout.name == "__timeout"
        assert "__timeout" in review.handles

    def test_the_old_spelling_says_what_to_use(self):
        _, _, review = self.form()

        with pytest.raises(KeyError, match=r"use \.timeout"):
            review.case("timeout")

    def test_the_edge_carries_dify_s_name(self):
        wf, start, review = self.form()
        wf.connect(review.case("ok"), wf.end({"o": start["q"]}, id="done"))
        wf.connect(review.timeout, wf.end({"o": start["q"]}, id="late"))

        assert ("review", "late", "__timeout") in _edges(wf)


class TestEveryBodyNodeStartsFromTheMarker:
    """The start marker was wired to the first body node alone. A second node
    reading only ``each.item`` had no way in, so every pass returned ``None``
    for it."""

    def test_two_nodes_reading_the_item_both_run(self):
        wf = Workflow("both")
        start = wf.start([text_input("q")])
        split = wf.code(
            "def main(s): return {'l': s.split(',')}",
            variables={"s": start["q"]},
            outputs={"l": "array[string]"},
            id="split",
        )
        with wf.iteration(split["l"]) as each:
            a = wf.template("A{{ n }}", variables={"n": each.item}, id="a")
            b = wf.template("B{{ n }}", variables={"n": each.item}, id="b")
            joined = wf.template(
                "{{ x }}+{{ y }}", variables={"x": a.output, "y": b.output}, id="j"
            )
            each.returns(joined.output)
        wf.end({"all": each.output})

        marker = f"{each.id}start"
        assert {(marker, "a"), (marker, "b")} <= {
            (e.source, e.target) for e in wf.edges
        }
        result = wf.run({"q": "x,y"}, code=StubCode({"l": ["x", "y"]}))
        assert result.outputs["all"] == ["Ax+Bx", "Ay+By"]

    def test_a_node_reached_inside_the_body_is_not_also_started(self):
        wf = Workflow("chain")
        start = wf.start([text_input("q")])
        with wf.iteration(start["q"]) as each:
            a = wf.template("{{ n }}", variables={"n": each.item}, id="a")
            b = wf.template("{{ n }}", variables={"n": a.output}, id="b")
            each.returns(b.output)
        wf.end({"all": each.output})

        marker = f"{each.id}start"
        assert (marker, "b") not in {(e.source, e.target) for e in wf.edges}


def test_a_node_with_a_way_out_but_none_in_is_refused():
    """It is connected, so the orphan check passed it, and it never ran."""
    wf = Workflow("stranded")
    start = wf.start([text_input("q")])
    lost = wf.template("lost", id="lost")
    wf.connect(lost, wf.end({"o": start["q"]}))
    wf.connect(start, wf.nodes[-1])

    with pytest.raises(WorkflowError, match="Nothing leads into lost"):
        wf.to_dict()


class TestAContainerStillAnswersTheAttributeProtocol:
    """Asking a loop for ``item`` raised a plain ``WorkflowError``, so
    ``hasattr`` raised instead of answering and ``getattr(..., None)`` never
    returned its default — anything introspecting nodes blew up on one."""

    def loop(self):
        wf = Workflow("introspect")
        start = wf.start([text_input("q")])
        with wf.loop(count=2) as body:
            wf.template("{{ q }}", variables={"q": start["q"]})
        return body

    def test_hasattr_answers(self):
        assert hasattr(self.loop(), "item") is False

    def test_getattr_returns_its_default(self):
        assert getattr(self.loop(), "item", None) is None

    def test_asking_directly_still_says_which_container_has_it(self):
        with pytest.raises(WorkflowError, match="Only an iteration has item"):
            self.loop().item


class TestNoInferredEdgeGoesAroundABranch:
    """The node wired to an arm was left alone, but one further down that also
    read the start node got an edge from it. Dify skips a node only when every
    way in was skipped, so it ran whichever arm was taken."""

    def arms(self):
        wf = Workflow("arms")
        start = wf.start([text_input("q")])
        branch = wf.if_else([when(start["q"], "is", "yes")], id="br")
        esc = wf.template("E{{ q }}", variables={"q": start["q"]}, id="esc")
        que = wf.template("Q{{ q }}", variables={"q": start["q"]}, id="que")
        wf.connect(branch.true, esc)
        wf.connect(branch.false, que)
        return wf, start, esc, que

    def test_a_node_after_an_arm_is_not_reached_from_before_the_branch(self):
        wf, start, esc, _ = self.arms()
        after = wf.template(
            "{{ e }} {{ q }}", variables={"e": esc.output, "q": start["q"]}, id="after"
        )
        wf.end({"a": after.output})

        pairs = {(e.source, e.target) for e in wf.edges}
        assert ("esc", "after") in pairs
        assert ("start", "after") not in pairs

    def test_it_runs_only_when_its_arm_is_taken(self):
        wf, start, esc, que = self.arms()
        after = wf.template(
            "{{ e }} {{ q }}", variables={"e": esc.output, "q": start["q"]}, id="after"
        )
        merged = wf.aggregate([after.output, que.output], id="merged")
        wf.end({"a": merged.output})

        assert wf.run({"q": "yes"}).outputs["a"] == "Eyes yes"
        assert wf.run({"q": "nope"}).outputs["a"] == "Qnope"

    def test_a_node_after_the_arms_rejoin_still_runs_either_way(self):
        wf, start, esc, que = self.arms()
        merged = wf.aggregate([esc.output, que.output], id="merged")
        tail = wf.template(
            "{{ m }}/{{ q }}",
            variables={"m": merged.output, "q": start["q"]},
            id="tail",
        )
        wf.end({"a": tail.output})

        assert ("start", "tail") not in {(e.source, e.target) for e in wf.edges}
        assert wf.run({"q": "yes"}).outputs["a"] == "Eyes/yes"
        assert wf.run({"q": "nope"}).outputs["a"] == "Qnope/nope"


class TestBreakConditionsAddUp:
    """``body.until()`` replaced what ``wf.loop(until=...)`` was given, though
    the docstring presented the two as complementary."""

    def test_both_sets_are_kept(self):
        wf = Workflow("both")
        start = wf.start([text_input("q")])
        with wf.loop(count=3, until=[when(start["q"], "is", "stop")]) as body:
            step = wf.template("{{ q }}", variables={"q": start["q"]}, id="step")
            body.until([when(step.output, "contains", "done")])

        selectors = [c.variable_selector for c in body.data.break_conditions]
        assert selectors == [["start", "q"], ["step", "output"]]

    def test_a_second_call_adds_too(self):
        wf = Workflow("twice")
        start = wf.start([text_input("q")])
        with wf.loop(count=3) as body:
            step = wf.template("{{ q }}", variables={"q": start["q"]}, id="step")
            body.until([when(step.output, "contains", "a")])
            body.until([when(step.output, "contains", "b")])

        assert len(body.data.break_conditions) == 2

    def test_a_different_operator_once_there_are_conditions_is_refused(self):
        wf = Workflow("clash")
        start = wf.start([text_input("q")])
        with pytest.raises(WorkflowError, match="one operator for all of them"):
            with wf.loop(count=3, until=[when(start["q"], "is", "x")]) as body:
                step = wf.template("{{ q }}", variables={"q": start["q"]}, id="step")
                body.until([when(step.output, "contains", "y")], logical="or")


def test_a_case_cannot_be_called_what_the_else_arm_is_called():
    """graphon continues along "false" when no case matched, so a case keyed
    "false" gave the branch two arms with one handle, and one never ran."""
    wf = Workflow("elif")
    start = wf.start([text_input("q")])

    with pytest.raises(WorkflowError, match="cannot also name a case"):
        wf.if_else(
            {
                "true": [when(start["q"], "is", "a")],
                "false": [when(start["q"], "is", "b")],
            }
        )
