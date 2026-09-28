"""Running code nodes locally, without Dify's sandbox service.

A code node normally reaches out to `dify-sandbox`, so in a local test it fails
with a connection error and the workflow stops. These executors close that gap
by running the node's code on this machine instead.

What they run is not a re-implementation: graphon's own template transformer
builds the program, so the source executed here is byte-identical to what Dify
would have sent to its sandbox.

Two things differ, and the second matters more than it looks.

The **confinement**: Dify isolates with seccomp, a Linux kernel facility that
does not exist on macOS, so ``LocalSandbox`` uses whatever the host offers —
Seatbelt on macOS, bubblewrap on Linux. Logic behaves the same; a program that
trips one confinement's limits will not necessarily trip the other's.

The **import surface**: Dify's sandbox sees its own interpreter and whatever
packages that deployment installed — not your project's virtualenv. Running a
code node against your own site-packages is how ``import pandas`` passes in a
test and fails in production, so ``LocalSandbox`` hides them by default and
takes the ones your Dify actually has as ``packages=[...]``.

If you do need the exact confinement, a ``dify-sandbox`` you already run
answers over HTTP and needs nothing from this module — pass its address as
``run(credentials={"code": {"execution_endpoint": ..., "execution_api_key": ...}})``.
"""

from __future__ import annotations

import os
import platform
import shutil

# Running a program under confinement is what this module is for.
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Generator

from graphon.dsl.code_runtime import SandboxCodeExecutionError, _transformer_for

#: How long a single code node may run before it is killed.
DEFAULT_TIMEOUT = 30.0

# Reads are allowed so the interpreter can load its own standard library; the
# program itself is handed over as a file the parent wrote before entering.
_SEATBELT_POLICY = """(version 1)
(deny default)
(allow process-exec process-fork)
(allow sysctl-read)
(allow file-read*)
(deny file-write*)
(allow file-write-data
  (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))
{network}
"""

_SEATBELT_NETWORK = {True: "(allow network*)", False: "(deny network*)"}


class SandboxUnavailable(SandboxCodeExecutionError):
    """Raised when this host offers no way to confine the code."""


class StubCode:
    """Answer code nodes with fixed outputs, without running anything.

    The counterpart of ``StubLLM``, for the same reason: it makes the rest of
    the graph testable when the node itself is not what is under test::

        wf.run(inputs, code=StubCode({"score": 0.9}))
        wf.run(inputs, code=StubCode({"tally": {"score": 0.9}}))   # per node id
    """

    def __init__(self, outputs: Mapping[str, Any] | None = None):
        self._outputs = dict(outputs or {})
        #: Every ``(code, inputs)`` this stub was asked to run.
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def execute(
        self,
        *,
        language: Any,
        code: str,
        inputs: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append((code, dict(inputs)))
        return dict(self._outputs)

    def is_execution_error(self, error: Exception) -> bool:
        return isinstance(error, SandboxCodeExecutionError)


class LocalSandbox:
    """Run a code node's program on this machine, confined by the OS.

    ``network`` is off by default, which is stricter than Dify's own sandbox
    (its shipped config sets ``enable_network: True``). A code node that makes
    requests will therefore fail here until you pass ``network=True`` — a
    visible failure rather than a machine quietly reaching the internet during
    a test run.

    ``packages`` names what the node may import beyond the standard library.
    The default is nothing: your project's virtualenv is hidden, because a code
    node that can reach it would import packages Dify's sandbox has never heard
    of and pass a test it should fail. Name what your Dify actually has::

        LocalSandbox(packages=["httpx"])     # the stock image ships httpx

    Pass ``confine=False`` to skip isolation entirely. That runs the workflow's
    code with the full privileges of the test process, so only do it for code
    you wrote and on a host you are willing to hand to it.
    """

    def __init__(
        self,
        *,
        packages: Sequence[str] = (),
        network: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        python: str | None = None,
        confine: bool = True,
    ):
        self.packages = tuple(packages)
        self.network = network
        self.timeout = timeout
        self.python = python or sys.executable
        self.confine = confine

    @staticmethod
    @lru_cache(maxsize=1)
    def available() -> bool:
        """Whether this host can actually confine code, tried rather than guessed.

        The presence of ``bwrap`` is not the same as being allowed to use it:
        Ubuntu 24.04 restricts unprivileged user namespaces, and a container
        may block them outright. So this runs one trivial confined program and
        reports what happened — which is the difference between a test that
        skips on a host without a sandbox and one that fails on it.
        """
        try:
            LocalSandbox(timeout=30.0)._run("print('{}')")
        except (SandboxUnavailable, OSError):
            return False
        except Exception:  # noqa: BLE001 - anything else is also "no"
            return False
        return True

    def importable(self) -> list[str]:
        """Top-level modules a code node will be able to import, beyond stdlib."""
        return sorted(_resolve_packages(self.packages))

    # -- CodeExecutorProtocol ---------------------------------------------

    def execute(
        self,
        *,
        language: Any,
        code: str,
        inputs: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        transformer = _transformer_for(language)
        program = transformer.build_program(code=code, inputs=inputs)
        if str(getattr(program, "language", language)) != "python3":
            msg = (
                f"LocalSandbox runs python3 only, not {program.language!r}. "
                "Run dify-sandbox in Docker for the other languages."
            )
            raise SandboxCodeExecutionError(msg)
        stdout = self._run(program.code)
        return transformer.parse_result(stdout)

    def is_execution_error(self, error: Exception) -> bool:
        return isinstance(error, SandboxCodeExecutionError)

    # -- internals ---------------------------------------------------------

    def _run(self, source: str) -> str:
        with tempfile.TemporaryDirectory(prefix="dify-code-") as work:
            script = Path(work) / "node.py"
            script.write_text(source, encoding="utf-8")
            libs = _link_packages(self.packages, Path(work) / "libs")
            command = self._command(script, Path(work))
            try:
                # The argv is built here, never a shell, never from input.
                completed = subprocess.run(  # noqa: S603  # nosec B603
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=work,
                    env=self._env(libs),
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                msg = f"The code node ran longer than {self.timeout}s and was stopped."
                raise SandboxCodeExecutionError(msg) from error

        if completed.returncode != 0:
            raise SandboxCodeExecutionError(self._failure(completed.stderr))
        return completed.stdout

    def _failure(self, stderr: str) -> str:
        """Lead with what to do, then the tail of the traceback.

        A full interpreter traceback buries the actionable line under frames
        from the standard library, so the cause goes first and the frames that
        actually name the user's code follow.
        """
        raw = (stderr or "").strip()
        detail = _last_frames(raw) if raw else "no output on stderr"
        if not self.network and _looks_like_a_blocked_request(raw):
            return (
                "The code node tried to use the network, which LocalSandbox "
                "blocks by default. Dify's own sandbox allows it, so pass "
                "LocalSandbox(network=True) if this node is meant to make "
                f"requests.\n\n{detail}"
            )
        return f"The code node failed:\n{detail}"

    def _command(self, script: Path, work: Path) -> list[str]:
        if not self.confine:
            return [self.python, "-S", str(script)]

        system = platform.system()
        if system == "Darwin":
            policy = work / "policy.sb"
            policy.write_text(
                _SEATBELT_POLICY.format(network=_SEATBELT_NETWORK[self.network]),
                encoding="utf-8",
            )
            return [
                "/usr/bin/sandbox-exec",
                "-f",
                str(policy),
                self.python,
                "-S",
                str(script),
            ]

        if system == "Linux" and shutil.which("bwrap"):
            # The "/tmp" below is a fresh tmpfs mounted over /tmp *inside* the
            # sandbox — the opposite of writing to a predictable temp path.
            command = [  # nosec B108
                "bwrap",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",
                "--bind",
                str(work),
                str(work),
                "--die-with-parent",
            ]
            if not self.network:
                command.append("--unshare-net")
            return [*command, self.python, "-S", str(script)]

        msg = (
            f"No way to confine code on this host ({system}). macOS uses "
            "sandbox-exec and Linux uses bubblewrap (apt install bubblewrap). "
            "Run dify-sandbox in Docker instead, or pass "
            "LocalSandbox(confine=False) to run unconfined."
        )
        raise SandboxUnavailable(msg)

    def _env(self, libs: Path) -> dict[str, str]:
        """A minimal environment, so the test process's secrets are not inherited."""
        keep = ("PATH", "LANG", "LC_ALL", "TMPDIR", "HOME")
        env = {name: os.environ[name] for name in keep if name in os.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        # -S keeps site-packages out; PYTHONPATH then adds back exactly the
        # packages the caller said their Dify has.
        env["PYTHONPATH"] = str(libs)
        return env


def _requirement_name(requirement: str) -> str | None:
    """The distribution name out of a requirement line, or None to skip it.

    Requirements that only apply to an extra are skipped: naming a package does
    not mean installing everything it could optionally pull in.
    """
    if "extra ==" in requirement:
        return None
    name = requirement.split(";", 1)[0].strip()
    for separator in ("[", "(", "<", ">", "=", "!", "~", " "):
        name = name.split(separator, 1)[0]
    return name.strip() or None


def _resolve_packages(packages: Sequence[str]) -> set[str]:
    """Top-level module names for ``packages`` and everything they need.

    A package is useless without its dependencies, so naming ``httpx`` brings
    httpcore, certifi and the rest with it — the caller should not have to know
    the tree.
    """
    from importlib import metadata

    seen: set[str] = set()
    modules: set[str] = set()
    named = {name for name in packages if name}
    queue = [(name, True) for name in named]

    while queue:
        name, was_named = queue.pop()
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        seen.add(key)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            if not was_named:
                # A dependency whose environment marker excludes it here, such
                # as a backport for an older Python. Nothing to link.
                continue
            msg = (
                f"packages names {name!r}, which is not installed here, so a "
                "code node could not import it either. Install it in this "
                "environment, or drop it from packages."
            )
            raise SandboxUnavailable(msg) from None

        modules.update(_top_level_modules(dist))
        for requirement in dist.requires or ():
            required = _requirement_name(requirement)
            if required:
                queue.append((required, False))

    return modules


def _top_level_modules(dist: Any) -> set[str]:
    """The importable names a distribution installs."""
    declared = dist.read_text("top_level.txt")
    if declared:
        return {line.strip() for line in declared.splitlines() if line.strip()}
    # Older or wheel-only distributions may not ship top_level.txt; fall back
    # to the first path segment of everything it installed.
    names: set[str] = set()
    for file in dist.files or ():
        head = str(file).split("/", 1)[0]
        if head.endswith(".py"):
            names.add(head[:-3])
        elif head and not head.endswith((".dist-info", ".data", ".pth")):
            names.add(head)
    return names


def _link_packages(packages: Sequence[str], destination: Path) -> Path:
    """Build a directory holding just the modules a code node may import."""
    destination.mkdir(parents=True, exist_ok=True)
    if not packages:
        return destination

    roots = [Path(path) for path in sys.path if path.endswith("site-packages")]
    for module in _resolve_packages(packages):
        for root in roots:
            for candidate in (root / module, root / f"{module}.py"):
                if candidate.exists():
                    target = destination / candidate.name
                    if not target.exists():
                        target.symlink_to(candidate)
                    break
            else:
                continue
            break
    return destination


def _last_frames(stderr: str, *, lines: int = 6) -> str:
    """The tail of a traceback, which is where the user's own code appears."""
    tail = stderr.strip().splitlines()[-lines:]
    return "\n".join(tail)


def _looks_like_a_blocked_request(stderr: str) -> bool:
    markers = (
        "URLError",
        "ConnectionError",
        "socket.gaierror",
        "Network is unreachable",
    )
    return any(marker in stderr for marker in markers)


@contextmanager
def code_executor(executor: Any) -> Generator[None, None, None]:
    """Make every code node in a workflow use ``executor`` for the duration."""
    import graphon.dsl.node_factory as node_factory

    original = node_factory.SandboxCodeExecutor
    # Patching a module attribute that happens to be a class; mypy reads the
    # rebinding as reassigning the type itself.
    node_factory.SandboxCodeExecutor = lambda _settings=None: executor  # type: ignore[misc,assignment]
    try:
        yield
    finally:
        node_factory.SandboxCodeExecutor = original  # type: ignore[misc]
