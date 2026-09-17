"""Every Service-API route Dify serves, and the call that reaches it.

This is the test the SDK most needed and did not have. Coverage was checked by
hand, twice, and the second rewrite silently dropped eleven routes anyway —
tags, the RAG pipeline, half the metadata, file download, pinning a run to a
published version. A hand check does not survive a refactor; this does.

Each route is exercised against a transport that records the path, so a method
that names a route it no longer reaches fails here rather than in production.
"""

import os
from pathlib import Path

import httpx
import pytest

from dify_client import DifyApp, DifyKnowledge
from dify_client.resources.knowledge import Dataset, Document, Segment

DATASET = Dataset(id="ds")
DOCUMENT = Document(id="doc", batch="batch-1")
SEGMENT = Segment(id="seg")


class Recorder:
    """Answers anything, and remembers what it was asked."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path.replace("/v1", "", 1)))
        return httpx.Response(
            200,
            json={
                "data": [],
                "fields": [],
                "records": [],
                "doc_metadata": [],
                "id": "x",
                "token": "t",
                "answer": "",
                "text": "",
                "document": {"id": "doc"},
                "batch": "batch-1",
                "task_id": "t",
                "limit": 20,
                "has_more": False,
            },
        )

    def app(self) -> DifyApp:
        return DifyApp(
            "k",
            user="u",
            http_client=httpx.Client(
                transport=httpx.MockTransport(self.handler), base_url="https://x/v1"
            ),
        )

    def knowledge(self) -> DifyKnowledge:
        return DifyKnowledge(
            "k",
            http_client=httpx.Client(
                transport=httpx.MockTransport(self.handler), base_url="https://x/v1"
            ),
        )


def note(tmp_path: Path) -> Path:
    path = tmp_path / "note.txt"
    path.write_text("x")
    return path


#: (route, verb, what reaches it). The route is spelled as Dify's controllers
#: spell it, with ids replaced by the value the call uses.
APP_ROUTES = [
    ("/", "GET", lambda a, _: a.server_info()),
    ("/info", "GET", lambda a, _: a.info()),
    ("/parameters", "GET", lambda a, _: a.parameters()),
    ("/meta", "GET", lambda a, _: a.meta()),
    ("/site", "GET", lambda a, _: a.site()),
    ("/app/feedbacks", "GET", lambda a, _: a.feedbacks()),
    ("/end-users/eu", "GET", lambda a, _: a.end_user("eu")),
    # chat
    ("/chat-messages", "POST", lambda a, _: a.chat.messages.create("hi")),
    ("/chat-messages/t/stop", "POST", lambda a, _: a.chat.messages.stop("t")),
    ("/messages", "GET", lambda a, _: a.chat.messages.list("c")),
    (
        "/messages/m/feedbacks",
        "POST",
        lambda a, _: a.chat.messages.feedback("m", "like"),
    ),
    ("/messages/m/suggested", "GET", lambda a, _: a.chat.messages.suggested("m")),
    ("/conversations", "GET", lambda a, _: a.chat.conversations.list()),
    ("/conversations/c", "DELETE", lambda a, _: a.chat.conversations.delete("c")),
    (
        "/conversations/c/name",
        "POST",
        lambda a, _: a.chat.conversations.rename("c", "n"),
    ),
    (
        "/conversations/c/variables",
        "GET",
        lambda a, _: a.chat.conversations.variables("c"),
    ),
    (
        "/conversations/c/variables/v",
        "PUT",
        lambda a, _: a.chat.conversations.set_variable("c", "v", 1),
    ),
    # completion
    ("/completion-messages", "POST", lambda a, _: a.completions.create({"query": "x"})),
    ("/completion-messages/t/stop", "POST", lambda a, _: a.completions.stop("t")),
    # workflow
    ("/workflows/run", "POST", lambda a, _: a.workflows.runs.create({})),
    (
        "/workflows/wf/run",
        "POST",
        lambda a, _: a.workflows.runs.create({}, workflow_id="wf"),
    ),
    ("/workflows/run/r", "GET", lambda a, _: a.workflows.runs.retrieve("r")),
    ("/workflows/tasks/t/stop", "POST", lambda a, _: a.workflows.runs.stop("t")),
    ("/workflows/logs", "GET", lambda a, _: a.workflows.runs.logs()),
    ("/workflow/r/events", "GET", lambda a, _: a.workflows.runs.events("r").close()),
    # human input
    ("/form/human_input/tok", "GET", lambda a, _: a.forms.retrieve("tok")),
    (
        "/form/human_input/tok",
        "POST",
        lambda a, _: a.forms.submit("tok", {}, action="ok"),
    ),
    # files
    ("/files/upload", "POST", lambda a, tmp: a.files.upload(note(tmp))),
    ("/files/f/preview", "GET", lambda a, _: a.files.download("f")),
    # annotations
    ("/apps/annotations", "GET", lambda a, _: a.annotations.list()),
    ("/apps/annotations", "POST", lambda a, _: a.annotations.create("q", "a")),
    ("/apps/annotations/an", "PUT", lambda a, _: a.annotations.update("an", "q", "a")),
    ("/apps/annotations/an", "DELETE", lambda a, _: a.annotations.delete("an")),
    (
        "/apps/annotation-reply/enable",
        "POST",
        lambda a, _: a.annotations.set_reply(True),
    ),
    (
        "/apps/annotation-reply/enable/status/j",
        "GET",
        lambda a, _: a.annotations.reply_status("j"),
    ),
    # audio
    ("/text-to-audio", "POST", lambda a, _: a.audio.speak("hi")),
    ("/audio-to-text", "POST", lambda a, tmp: a.audio.transcribe(note(tmp))),
]

KNOWLEDGE_ROUTES = [
    # Guarded by the dataset token, not the app key — which is why it is on the
    # knowledge client and not on DifyApp.
    ("/workspaces/current/models/model-types/llm", "GET", lambda k, _: k.models()),
    ("/datasets", "POST", lambda k, _: k.datasets.create("n")),
    ("/datasets", "GET", lambda k, _: k.datasets.list()),
    ("/datasets/ds", "GET", lambda k, _: k.datasets.retrieve(DATASET)),
    ("/datasets/ds", "PATCH", lambda k, _: k.datasets.update(DATASET, name="n")),
    ("/datasets/ds", "DELETE", lambda k, _: k.datasets.delete(DATASET)),
    ("/datasets/ds/retrieve", "POST", lambda k, _: k.datasets.search(DATASET, "q")),
    ("/datasets/ds/tags", "GET", lambda k, _: k.datasets.tags(DATASET)),
    # metadata
    ("/datasets/ds/metadata", "GET", lambda k, _: k.datasets.metadata(DATASET)),
    (
        "/datasets/ds/metadata",
        "POST",
        lambda k, _: k.datasets.add_metadata_field(DATASET, "f"),
    ),
    (
        "/datasets/ds/metadata/m",
        "PATCH",
        lambda k, _: k.datasets.rename_metadata_field(DATASET, "m", "n"),
    ),
    (
        "/datasets/ds/metadata/m",
        "DELETE",
        lambda k, _: k.datasets.delete_metadata_field(DATASET, "m"),
    ),
    (
        "/datasets/ds/metadata/built-in",
        "GET",
        lambda k, _: k.datasets.built_in_metadata(DATASET),
    ),
    (
        "/datasets/ds/metadata/built-in/enable",
        "POST",
        lambda k, _: k.datasets.set_built_in_metadata(DATASET, True),
    ),
    # documents
    (
        "/datasets/ds/document/create-by-text",
        "POST",
        lambda k, _: k.documents(DATASET).create(text="t", name="n"),
    ),
    (
        "/datasets/ds/document/create-by-file",
        "POST",
        lambda k, tmp: k.documents(DATASET).create(file=note(tmp)),
    ),
    ("/datasets/ds/documents", "GET", lambda k, _: k.documents(DATASET).list()),
    (
        "/datasets/ds/documents/doc",
        "GET",
        lambda k, _: k.documents(DATASET).retrieve(DOCUMENT),
    ),
    (
        "/datasets/ds/documents/doc",
        "PATCH",
        lambda k, tmp: k.documents(DATASET).update(DOCUMENT, file=note(tmp)),
    ),
    (
        "/datasets/ds/documents/doc",
        "DELETE",
        lambda k, _: k.documents(DATASET).delete(DOCUMENT),
    ),
    (
        "/datasets/ds/documents/doc/update-by-text",
        "POST",
        lambda k, _: k.documents(DATASET).update(DOCUMENT, text="t"),
    ),
    (
        "/datasets/ds/documents/batch-1/indexing-status",
        "GET",
        lambda k, _: k.documents(DATASET).indexing_status(DOCUMENT),
    ),
    (
        "/datasets/ds/documents/doc/download",
        "GET",
        lambda k, _: k.documents(DATASET).download(DOCUMENT),
    ),
    (
        "/datasets/ds/documents/download-zip",
        "POST",
        lambda k, _: k.documents(DATASET).download_all([DOCUMENT]),
    ),
    (
        "/datasets/ds/documents/metadata",
        "POST",
        lambda k, _: k.documents(DATASET).set_metadata([]),
    ),
    (
        "/datasets/ds/documents/status/enable",
        "PATCH",
        lambda k, _: k.documents(DATASET).set_enabled([DOCUMENT], True),
    ),
    # segments
    (
        "/datasets/ds/documents/doc/segments",
        "GET",
        lambda k, _: k.documents(DATASET).segments(DOCUMENT).list(),
    ),
    (
        "/datasets/ds/documents/doc/segments",
        "POST",
        lambda k, _: k.documents(DATASET).segments(DOCUMENT).create([]),
    ),
    (
        "/datasets/ds/documents/doc/segments/seg",
        "GET",
        lambda k, _: k.documents(DATASET).segments(DOCUMENT).retrieve(SEGMENT),
    ),
    (
        "/datasets/ds/documents/doc/segments/seg",
        "POST",
        lambda k, _: k.documents(DATASET)
        .segments(DOCUMENT)
        .update(SEGMENT, content="c"),
    ),
    (
        "/datasets/ds/documents/doc/segments/seg",
        "DELETE",
        lambda k, _: k.documents(DATASET).segments(DOCUMENT).delete(SEGMENT),
    ),
    (
        "/datasets/ds/documents/doc/segments/seg/child_chunks",
        "GET",
        lambda k, _: k.documents(DATASET).segments(DOCUMENT).child_chunks(SEGMENT),
    ),
    (
        "/datasets/ds/documents/doc/segments/seg/child_chunks",
        "POST",
        lambda k, _: k.documents(DATASET)
        .segments(DOCUMENT)
        .add_child_chunk(SEGMENT, "c"),
    ),
    (
        "/datasets/ds/documents/doc/segments/seg/child_chunks/cc",
        "PATCH",
        lambda k, _: k.documents(DATASET)
        .segments(DOCUMENT)
        .update_child_chunk(SEGMENT, "cc", "c"),
    ),
    (
        "/datasets/ds/documents/doc/segments/seg/child_chunks/cc",
        "DELETE",
        lambda k, _: k.documents(DATASET)
        .segments(DOCUMENT)
        .delete_child_chunk(SEGMENT, "cc"),
    ),
    # tags
    ("/datasets/tags", "GET", lambda k, _: k.tags.list()),
    ("/datasets/tags", "POST", lambda k, _: k.tags.create("t")),
    ("/datasets/tags", "PATCH", lambda k, _: k.tags.rename("t", "n")),
    ("/datasets/tags", "DELETE", lambda k, _: k.tags.delete("t")),
    ("/datasets/tags/binding", "POST", lambda k, _: k.tags.bind(DATASET, ["t"])),
    ("/datasets/tags/unbinding", "POST", lambda k, _: k.tags.unbind(DATASET, "t")),
    # pipeline
    (
        "/datasets/ds/pipeline/datasource-plugins",
        "GET",
        lambda k, _: k.pipeline(DATASET).datasources(),
    ),
    (
        "/datasets/ds/pipeline/datasource/nodes/n/run",
        "POST",
        lambda k, _: k.pipeline(DATASET).run_datasource(
            "n", datasource_type="local_file"
        ),
    ),
    (
        "/datasets/ds/pipeline/run",
        "POST",
        lambda k, _: k.pipeline(DATASET).run(
            start_node_id="s", datasource_type="local_file", datasource_info_list=[]
        ),
    ),
    (
        "/datasets/pipeline/file-upload",
        "POST",
        lambda k, tmp: k.upload_for_pipeline(note(tmp)),
    ),
]


@pytest.mark.parametrize(
    ("route", "verb", "call"), APP_ROUTES, ids=lambda v: v if isinstance(v, str) else ""
)
def test_an_app_route_is_reached(route, verb, call, tmp_path):
    recorder = Recorder()
    call(recorder.app(), tmp_path)
    assert (verb, route) in recorder.calls, recorder.calls


@pytest.mark.parametrize(
    ("route", "verb", "call"),
    KNOWLEDGE_ROUTES,
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_a_knowledge_route_is_reached(route, verb, call, tmp_path):
    recorder = Recorder()
    call(recorder.knowledge(), tmp_path)
    assert (verb, route) in recorder.calls, recorder.calls


class TestTheListIsComplete:
    """The list above must keep up with Dify, not drift from it.

    Read out of Dify's own controllers, so "the SDK covers the Service API" is
    a checked claim rather than a remembered one. CI clones the version in
    `tests/DIFY_VERSION` to `../dify-oss`; without a checkout these skip, which
    is honest locally and caught by `test_ci_provides_the_checkout` for CI.
    """

    #: Where the checkout is expected. CI puts it there; so does a developer
    #: who cloned Dify beside this repo.
    CONTROLLERS = (
        Path(os.environ.get("DIFY_SOURCE", "../dify-oss"))
        / "api/controllers/service_api"
    )

    #: The Dify this SDK is verified against.
    VERSION = (Path(__file__).parent / "DIFY_VERSION").read_text().strip()

    #: Routes with no method on purpose, and why. Every entry names a route
    #: Dify itself marks deprecated or duplicated — never one that is merely
    #: inconvenient.
    DELIBERATELY_ABSENT = {
        # Dify serves both spellings and deprecates the underscored one, in
        # `DeprecatedDocumentAddByTextApi` and friends. The SDK calls the other.
        "/datasets/<x>/document/create_by_text": "deprecated; create-by-text is canonical",
        "/datasets/<x>/document/create_by_file": "deprecated; create-by-file is canonical",
        "/datasets/<x>/documents/<x>/update_by_text": "deprecated; update-by-text is canonical",
        # Both spellings of this one are deprecated; PATCH on the document is
        # the route Dify keeps, and the only one this SDK calls.
        "/datasets/<x>/documents/<x>/update_by_file": "deprecated; PATCH /documents/<id> replaces it",
        "/datasets/<x>/documents/<x>/update-by-file": "deprecated too; PATCH /documents/<id> replaces it",
        "/datasets/<x>/hit-testing": "same Resource as /retrieve, which datasets.search calls",
    }

    #: The path segments the table above uses for ids, so a route written with
    #: real values can be compared against Dify's `<placeholder>` spelling.
    #: `enable` is in there because Dify spells those segments as parameters
    #: (`annotation-reply/<action>`) while the table uses a real value.
    _IDS = r"ds|doc|seg|cc|an|eu|wf|tok|batch-1|llm|enable|[a-z]"

    @staticmethod
    def _operations(controllers):
        """Every (verb, path, deprecated) Dify's Service API declares.

        Read with `ast`, not a regex over the source. The regex assumed a route
        decorator fits on one line, and quietly missed seven operations —
        four of them the current child-chunk API, which therefore looked
        covered when nothing was checking it.
        """
        import ast
        import re

        verbs = {"get", "post", "put", "patch", "delete"}
        found = set()
        for source in controllers.rglob("*.py"):
            tree = ast.parse(source.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                paths: list[str] = []
                deprecated_paths: set[str] = set()
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    called = decorator.func
                    if not (
                        isinstance(called, ast.Attribute) and called.attr == "route"
                    ):
                        continue
                    rendered = ast.unparse(decorator)
                    marked = (
                        "'deprecated': True" in rendered
                        or "deprecated=True" in rendered
                    )
                    for argument in decorator.args:
                        if isinstance(argument, ast.Constant) and isinstance(
                            argument.value, str
                        ):
                            paths.append(argument.value)
                            if marked:
                                deprecated_paths.add(argument.value)
                if not paths:
                    continue
                on_class = (
                    "Deprecated" in node.name
                    or "deprecated" in (ast.get_docstring(node) or "").lower()
                )
                declared = {
                    body.name
                    for body in node.body
                    if isinstance(body, ast.FunctionDef) and body.name in verbs
                }
                for path in paths:
                    generic = re.sub(r"<[^>]+>", "<x>", path).rstrip("/") or "/"
                    for verb in declared:
                        found.add(
                            (
                                verb.upper(),
                                generic,
                                on_class or path in deprecated_paths,
                            )
                        )
        return found

    def _served(self, controllers):
        return {(verb, path) for verb, path, _ in self._operations(controllers)}

    def _deprecated(self, controllers):
        return {
            (verb, path) for verb, path, gone in self._operations(controllers) if gone
        }

    def _covered(self):
        """Every (verb, path) the table above reaches, in Dify's spelling."""
        import re

        covered = set()
        for route, verb, _ in APP_ROUTES + KNOWLEDGE_ROUTES:
            generic = re.sub(rf"/(?:{self._IDS})(?=/|$)", "/<x>", route)
            covered.add((verb, generic.rstrip("/") or "/"))
        return covered

    def test_every_route_dify_serves_has_a_call(self):
        if not self.CONTROLLERS.is_dir():
            pytest.skip("no dify-oss checkout beside this repo")

        # Deprecated operations are exempt by being deprecated, not by being
        # on a hand-kept list — so a newly deprecated route needs no edit here.
        uncovered = sorted(
            self._served(self.CONTROLLERS)
            - self._covered()
            - self._deprecated(self.CONTROLLERS)
        )

        assert (
            uncovered == []
        ), "Dify serves these and nothing here reaches them: " + ", ".join(
            f"{v} {p}" for v, p in uncovered
        )

    def test_the_check_notices_a_dropped_call(self):
        """A coverage test that cannot fail is worse than none."""
        if not self.CONTROLLERS.is_dir():
            pytest.skip("no dify-oss checkout beside this repo")

        served = self._served(self.CONTROLLERS)
        assert served, "no routes were read out of the controllers"
        thinned = self._covered() - {("GET", "/datasets/tags")}
        assert ("GET", "/datasets/tags") in served - thinned

    def test_dify_marks_exactly_the_routes_this_exempts(self):
        """If Dify deprecates something new, this says so rather than the SDK
        quietly carrying on calling it."""
        if not self.CONTROLLERS.is_dir():
            pytest.skip("no dify-oss checkout beside this repo")

        deprecated = {path for _, path in self._deprecated(self.CONTROLLERS)}
        assert deprecated == set(self.DELIBERATELY_ABSENT) - {"/"}

    def test_nothing_deprecated_is_reached_by_default(self):
        """The SDK called the deprecated spelling of four routes, and the
        exemption list said the opposite of what Dify's source says."""
        import re
        import tempfile

        from dify_client import DifyKnowledge
        from dify_client.resources.knowledge import Dataset, Document

        touched: list[str] = []

        def handler(request):
            touched.append(
                re.sub(
                    r"/(?:ds|doc|seg)(?=/|$)",
                    "/<x>",
                    request.url.path.replace("/v1", "", 1),
                )
            )
            return httpx.Response(
                200,
                json={"document": {"id": "d"}, "batch": "b", "data": [], "records": []},
            )

        knowledge = DifyKnowledge(
            "k",
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler), base_url="https://x/v1"
            ),
        )
        dataset = Dataset(id="ds")
        document = Document(id="doc", name="n", batch="b")
        upload = Path(tempfile.mkdtemp()) / "a.txt"
        upload.write_text("x")

        docs = knowledge.documents(dataset)
        docs.create(text="t", name="n")
        docs.create(file=upload)
        docs.update(document, text="t")
        docs.update(document, file=upload)
        knowledge.datasets.search(dataset, "q")

        assert not set(touched) & set(self.DELIBERATELY_ABSENT), touched

    @staticmethod
    def _deprecated_fields(controllers):
        """Payload fields Dify marks deprecated, and the class they are on.

        A current route can carry a field that is not. `TagUnbindingPayload`
        accepts a singular `tag_id` and marks it deprecated in its own JSON
        schema, which a route-level check cannot see.
        """
        import ast
        import re

        found: dict[str, set[str]] = {}
        for source in controllers.rglob("*.py"):
            text = source.read_text()
            if '"deprecated": True' not in text:
                continue
            for node in ast.walk(ast.parse(text)):
                if not isinstance(node, ast.ClassDef) or "Payload" not in node.name:
                    continue
                rendered = ast.unparse(node)
                if "'deprecated': True" not in rendered:
                    continue
                names = set(
                    re.findall(
                        r"(\w+)_annotations\s*=\s*\{[^}]*'deprecated': True", rendered
                    )
                )
                if names:
                    found.setdefault(node.name, set()).update(names)
        return found

    #: Which SDK call sends each deprecated-payload route's body, so the check
    #: below looks at that call rather than at every use of the field's name —
    #: `tag_id` is deprecated on TagUnbindingPayload and current on
    #: TagUpdatePayload, and a bare string search cannot tell them apart.
    _PAYLOAD_SENDERS = {
        "TagUnbindingPayload": ("dify_client.resources.knowledge", "Tags", "unbind"),
    }

    def test_no_deprecated_payload_field_is_sent(self):
        """A route being current does not make every field on it current.

        `tags.unbind` sent the deprecated singular `tag_id`, which Dify still
        accepts — so it worked, and nothing said otherwise.
        """
        if not self.CONTROLLERS.is_dir():
            pytest.skip("no dify-oss checkout beside this repo")

        import importlib
        import inspect

        fields = self._deprecated_fields(self.CONTROLLERS.parent)
        assert fields, "no deprecated payload fields were read out of Dify"
        assert set(fields) <= set(self._PAYLOAD_SENDERS), (
            "Dify deprecated a payload field this does not know who sends: "
            + ", ".join(sorted(set(fields) - set(self._PAYLOAD_SENDERS)))
        )

        for owner, names in sorted(fields.items()):
            module_name, class_name, method = self._PAYLOAD_SENDERS[owner]
            module = importlib.import_module(module_name)
            source = inspect.getsource(getattr(getattr(module, class_name), method))
            for name in sorted(names):
                assert f'"{name}"' not in source, f"{owner}.{name} is deprecated"

    def test_the_field_check_knows_about_tag_id(self):
        """The one this caught. If Dify removes it, this says so."""
        if not self.CONTROLLERS.is_dir():
            pytest.skip("no dify-oss checkout beside this repo")

        fields = self._deprecated_fields(self.CONTROLLERS.parent)
        assert fields.get("TagUnbindingPayload") == {"tag_id"}

    def test_each_exemption_names_a_reason(self):
        assert all(self.DELIBERATELY_ABSENT.values())

    def test_the_pinned_version_is_recorded(self):
        assert self.VERSION

    def test_ci_provides_the_checkout(self):
        """These tests skip without Dify's source, and a skipped check proves
        nothing — so CI has to be the thing that supplies it."""
        workflow = Path(__file__).parent.parent / ".github/workflows/ci.yml"
        text = workflow.read_text()
        assert "langgenius/dify" in text, "CI does not clone Dify"
        assert "DIFY_VERSION" in text, "CI does not pin the version"

    def test_the_checkout_is_the_pinned_version(self):
        """A checkout of some other Dify would verify against the wrong API."""
        if not self.CONTROLLERS.is_dir():
            pytest.skip("no dify-oss checkout beside this repo")

        import subprocess

        root = self.CONTROLLERS.parents[2]
        described = subprocess.run(
            ["git", "-C", str(root), "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not described:
            pytest.skip("the checkout is not a git repository")
        assert described == self.VERSION, (
            f"checked out {described}, pinned {self.VERSION} — "
            "the coverage claim is about the pinned one"
        )
