"""Running code nodes without Dify's sandbox service."""

import platform
import shutil

import pytest

from dify_client.workflow import (
    LocalSandbox,
    SandboxUnavailable,
    StubCode,
    Workflow,
    text_input,
)
from dify_client.workflow.sandbox import _last_frames, code_executor

# Tried, not guessed: `bwrap` on PATH is not the same as being allowed to use
# it — Ubuntu 24.04 restricts unprivileged user namespaces — and a presence
# check would turn "this host cannot confine" into a failing test.
needs_confinement = pytest.mark.skipif(
    not LocalSandbox.available(),
    reason="this host cannot confine code (needs macOS or working bubblewrap)",
)


def code_workflow(source: str, outputs: dict[str, str]) -> Workflow:
    wf = Workflow("code-app")
    start = wf.start([text_input("q")])
    node = wf.code(source, variables={"q": start["q"]}, outputs=outputs, id="c")
    answer = wf.answer(node[next(iter(outputs))], id="answer")
    wf.connect(start, node, answer)
    return wf


LENGTH = "def main(q):\n    return {'n': len(q)}"
NETWORK = (
    "import urllib.request\n"
    "def main(q):\n"
    "    urllib.request.urlopen('https://example.com', timeout=5)\n"
    "    return {'r': 'reached'}"
)
WRITE = "def main(q):\n    open('/tmp/dify-sdk-escape.txt', 'w').write('x')\n    return {'r': 'wrote'}"


class TestWithoutAnExecutor:
    def test_a_code_node_reaches_for_difys_sandbox_service(self):
        """Left alone it speaks HTTP to dify-sandbox, wherever that is pointed.

        Whether that succeeds depends on the machine — a Dify stack running
        locally answers on the default endpoint — so this asserts the wiring
        rather than the outcome.
        """
        import graphon.dsl.node_factory as factory
        from graphon.dsl.code_runtime import SandboxCodeExecutor

        assert factory.SandboxCodeExecutor is SandboxCodeExecutor


class TestStubCode:
    def test_the_program_it_runs_is_the_one_dify_would_have(self):
        """graphon builds it, so local and remote execute the same source."""
        stub = StubCode({"n": 1})
        code_workflow(LENGTH, {"n": "number"}).run({"q": "hi"}, code=stub)
        assert stub.calls[0][0] == LENGTH

    def test_it_answers_without_running_anything(self):
        wf = code_workflow(LENGTH, {"n": "number"})
        result = wf.run({"q": "hello"}, code=StubCode({"n": 999}))
        assert result.node("c")["n"] == 999

    def test_it_records_what_it_was_asked_to_run(self):
        stub = StubCode({"n": 1})
        code_workflow(LENGTH, {"n": "number"}).run({"q": "hello"}, code=stub)
        source, inputs = stub.calls[0]
        assert "def main(q)" in source
        assert inputs == {"q": "hello"}


@needs_confinement
class TestLocalSandbox:
    """Everything here actually runs code under the OS sandbox, so it needs a
    host that can confine one. GitHub's runners cannot: `bwrap` is installed
    but the kernel refuses the network namespace it sets up
    (`RTM_NEWADDR: Operation not permitted`), and `available()` finds that out
    by trying rather than by looking for the binary."""

    def test_it_runs_the_node_and_returns_its_outputs(self):
        wf = code_workflow(LENGTH, {"n": "number"})
        result = wf.run({"q": "hello"}, code=LocalSandbox(), raise_on_error=True)
        assert result.node("c")["n"] == 5

    def test_an_error_in_the_node_names_the_user_line(self):
        wf = code_workflow("def main(q):\n    return {'r': 1 / 0}", {"r": "string"})
        result = wf.run({"q": "x"}, code=LocalSandbox())
        assert not result.succeeded
        assert "ZeroDivisionError" in result.error

    def test_a_runaway_node_is_stopped(self):
        wf = code_workflow(
            "import time\ndef main(q):\n    time.sleep(30)\n    return {'r':'x'}",
            {"r": "string"},
        )
        result = wf.run({"q": "x"}, code=LocalSandbox(timeout=2.0))
        assert not result.succeeded
        assert "longer than" in result.error


@needs_confinement
class TestConfinement:
    def test_the_network_is_blocked_by_default(self):
        result = code_workflow(NETWORK, {"r": "string"}).run(
            {"q": "x"}, code=LocalSandbox()
        )
        assert not result.succeeded

    def test_the_refusal_explains_the_difference_from_dify(self):
        """Dify's sandbox ships with enable_network: True, so say so."""
        result = code_workflow(NETWORK, {"r": "string"}).run(
            {"q": "x"}, code=LocalSandbox()
        )
        assert "network=True" in result.error
        assert "Dify's own sandbox allows it" in result.error

    def test_the_network_can_be_opened_deliberately(self):
        result = code_workflow(NETWORK, {"r": "string"}).run(
            {"q": "x"}, code=LocalSandbox(network=True)
        )
        assert result.succeeded

    def test_writing_outside_the_workspace_is_blocked(self):
        result = code_workflow(WRITE, {"r": "string"}).run(
            {"q": "x"}, code=LocalSandbox()
        )
        assert not result.succeeded
        assert "PermissionError" in result.error or "not permitted" in result.error

    def test_computation_is_untouched(self):
        wf = code_workflow(
            "def main(q):\n    return {'n': sum(range(1000))}", {"n": "number"}
        )
        result = wf.run({"q": "x"}, code=LocalSandbox(), raise_on_error=True)
        assert result.node("c")["n"] == 499500


IMPORT_PROBE = """
def main(q):
    import sys
    got = []
    for mod in ('httpx', 'pandas', 'graphon', 'json'):
        try:
            __import__(mod)
            got.append(mod)
        except Exception:
            pass
    return {'r': ','.join(got)}
"""


class TestImportSurface:
    """What a code node can import is the difference that bites in production.

    Dify's sandbox sees its own interpreter and that deployment's packages, not
    this project's virtualenv. A node that can reach the virtualenv passes a
    test it should fail.
    """

    def _importable(self, sandbox: LocalSandbox) -> set[str]:
        wf = code_workflow(IMPORT_PROBE, {"r": "string"})
        result = wf.run({"q": "x"}, code=sandbox, raise_on_error=True)
        return set(filter(None, result.node("c")["r"].split(",")))

    @needs_confinement
    def test_the_projects_packages_are_hidden_by_default(self):
        assert self._importable(LocalSandbox()) == {"json"}

    def test_this_process_can_import_them_even_so(self):
        """Proving the previous test is about the sandbox, not the machine."""
        import httpx  # noqa: F401
        import pandas  # noqa: F401

    @needs_confinement
    def test_a_declared_package_becomes_importable(self):
        assert self._importable(LocalSandbox(packages=["httpx"])) == {"httpx", "json"}

    @needs_confinement
    def test_declaring_one_package_does_not_open_the_rest(self):
        assert "pandas" not in self._importable(LocalSandbox(packages=["httpx"]))

    def test_dependencies_come_along(self):
        """httpx is useless without httpcore and certifi."""
        assert {"httpcore", "certifi"} <= set(
            LocalSandbox(packages=["httpx"]).importable()
        )

    def test_importable_reports_what_was_resolved(self):
        assert "httpx" in LocalSandbox(packages=["httpx"]).importable()
        assert LocalSandbox().importable() == []

    def test_naming_something_uninstalled_is_refused(self):
        with pytest.raises(SandboxUnavailable, match="not installed here"):
            LocalSandbox(packages=["definitely-not-installed"]).importable()

    def test_a_dependency_excluded_by_its_marker_is_skipped(self):
        """Backports for older Pythons are absent by design, not an error."""
        assert LocalSandbox(packages=["anyio"]).importable()


class TestUnconfined:
    def test_it_is_an_explicit_choice(self):
        wf = code_workflow(LENGTH, {"n": "number"})
        result = wf.run(
            {"q": "hello"}, code=LocalSandbox(confine=False), raise_on_error=True
        )
        assert result.node("c")["n"] == 5

    def test_a_host_without_confinement_says_what_to_do(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Plan9")
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        result = code_workflow(LENGTH, {"n": "number"}).run(
            {"q": "x"}, code=LocalSandbox()
        )
        assert not result.succeeded
        assert "confine=False" in result.error
        assert "bubblewrap" in result.error

    def test_the_unavailable_error_is_a_sandbox_error(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Plan9")
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        with pytest.raises(SandboxUnavailable):
            LocalSandbox().execute(language="python3", code=LENGTH, inputs={"q": "x"})


class TestIsolationOfThePatch:
    def test_the_executor_is_restored_afterwards(self):
        import graphon.dsl.node_factory as factory

        before = factory.SandboxCodeExecutor
        with code_executor(StubCode()):
            assert factory.SandboxCodeExecutor is not before
        assert factory.SandboxCodeExecutor is before

    def test_it_is_restored_after_a_failure(self):
        import graphon.dsl.node_factory as factory

        before = factory.SandboxCodeExecutor
        with pytest.raises(RuntimeError):  # noqa: PT012 - the raise is the point
            with code_executor(StubCode()):
                raise RuntimeError("boom")
        assert factory.SandboxCodeExecutor is before


class TestHelpers:
    def test_only_the_tail_of_a_traceback_is_kept(self):
        text = "\n".join(f"line {i}" for i in range(20))
        assert _last_frames(text, lines=3) == "line 17\nline 18\nline 19"
