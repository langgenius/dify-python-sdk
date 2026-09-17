"""What a `pip install dify-client` actually gets.

Packaging is the one part of a library nobody exercises while writing it, and
every mistake in it lands on someone else's machine: a dependency that is
installed and never used, a marker file that was left out so the annotations
are ignored, a stray file from the maintainer's working tree, a release
pipeline whose build step does not run.
"""

import tomllib
from pathlib import Path

import pytest

import dify_client

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def project():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


class TestTheMetadataIsTrue:
    def test_the_version_is_the_one_the_package_reports(self, project):
        """Two places would drift; this is which one wins."""
        assert dify_client.__version__ == project["version"]

    def test_the_readme_it_advertises_exists(self, project):
        assert (ROOT / project["readme"]).is_file()

    def test_the_licence_file_is_there(self):
        assert (ROOT / "LICENSE").is_file()

    def test_it_says_it_ships_types(self, project):
        assert "Typing :: Typed" in project["classifiers"]

    def test_and_it_actually_does(self):
        assert (ROOT / "dify_client" / "py.typed").is_file()

    def test_the_urls_point_at_this_package(self, project):
        assert "dify-python-sdk" in project["urls"]["Repository"]


class TestEveryDependencyEarnsItsPlace:
    """A declared dependency is installed on every machine that installs this.
    `aiofiles` and httpx's `http2` extra were both carried for a while without
    a single import between them."""

    #: Distribution name -> the module it provides.
    PROVIDES = {"httpx": "httpx", "pyyaml": "yaml", "graphon": "graphon"}

    @staticmethod
    def _sources():
        return [path.read_text() for path in (ROOT / "dify_client").rglob("*.py")]

    def _named(self, requirement: str) -> str:
        name = requirement.split(">")[0].split("=")[0].split("[")[0].split(";")[0]
        return name.strip().lower()

    def test_each_runtime_dependency_is_imported(self, project):
        sources = self._sources()
        for requirement in project["dependencies"]:
            name = self._named(requirement)
            module = self.PROVIDES.get(name, name)
            assert any(
                f"import {module}" in source for source in sources
            ), f"{name} is installed for everyone and imported by nobody"

    def test_the_dependency_list_is_known(self, project):
        """So a new one cannot be added without deciding what it provides."""
        for requirement in project["dependencies"]:
            assert self._named(requirement) in self.PROVIDES

    def test_no_extra_is_pulled_in_that_nothing_uses(self, project):
        """`httpx[http2]` installs `h2` on every machine. Nothing asks for
        HTTP/2 here."""
        assert not any("[" in requirement for requirement in project["dependencies"])

    def test_the_workflow_extra_is_the_only_optional_one(self, project):
        assert set(project["optional-dependencies"]) == {"workflow"}


class TestTheSdistCarriesWhatItShould:
    @pytest.fixture(scope="class")
    def hatch(self):
        return tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]

    def test_the_contents_are_named_rather_than_swept_up(self, hatch):
        """Hatchling otherwise includes whatever is lying in the working tree
        — a file written by running an example ended up in a build."""
        included = hatch["build"]["targets"]["sdist"]["include"]
        assert "/dify_client" in included
        assert "/LICENSE" in included

    def test_nothing_agent_shaped_is_shipped(self, hatch):
        """CLAUDE.md, AGENTS.md and .claude/ are instructions for working on
        this repo, not part of the package."""
        included = hatch["build"]["targets"]["sdist"]["include"]
        assert not any("claude" in entry.lower() for entry in included)
        assert not any("AGENTS" in entry for entry in included)

    def test_the_type_marker_is_an_artifact_of_the_wheel(self, hatch):
        assert "dify_client/py.typed" in hatch["build"]["targets"]["wheel"]["artifacts"]


class TestTheReleasePipelineRuns:
    """The build job failed on every release: `uv run build` looks for a
    command called `build`, and the package installs `pyproject-build`."""

    @pytest.fixture(scope="class")
    def workflow(self):
        return (ROOT / ".github/workflows/build.yml").read_text()

    @pytest.fixture(scope="class")
    def commands(self, workflow):
        """What the jobs actually run — the comments explain, they do not
        execute, and asserting on the file text made a comment about the bug
        look like the bug."""
        import yaml

        document = yaml.safe_load(workflow)
        return [
            step["run"]
            for job in document["jobs"].values()
            for step in job.get("steps", [])
            if "run" in step
        ]

    def test_it_builds_with_a_command_that_exists(self, commands):
        run = "\n".join(commands)
        assert "uv build" in run
        assert "uv run build\n" not in run

    def test_it_runs_the_tests_before_publishing(self, commands):
        """A release tag does not trigger ci.yml."""
        assert any("pytest" in command for command in commands)

    def test_it_checks_the_package_before_publishing(self, commands):
        assert any("twine check" in command for command in commands)

    def test_publishing_needs_no_stored_token(self, workflow):
        """Trusted publishing: GitHub mints a short-lived token for the run and
        PyPI checks where it came from. A stored API token is a credential that
        outlives the release it was made for."""
        import yaml

        publish = yaml.safe_load(workflow)["jobs"]["publish"]

        assert publish["permissions"]["id-token"] == "write"
        # On the parsed steps, not the file text: the comment above the job
        # explains which secret is no longer read, and matching on that made
        # the explanation look like the thing it explains.
        assert all(
            "password" not in step.get("with", {}) for step in publish["steps"]
        ), "a stored token is still being handed to the upload"

    def test_a_release_waits_for_a_person(self, workflow):
        """The environment is where the approval is configured; without it
        naming one, there is nothing for a reviewer to hold."""
        import yaml

        publish = yaml.safe_load(workflow)["jobs"]["publish"]

        assert publish["environment"]["name"] == "pypi"

    def test_it_only_publishes_a_release(self, workflow):
        import yaml

        publish = yaml.safe_load(workflow)["jobs"]["publish"]

        assert "release" in publish["if"]
        assert publish["needs"] == ["build", "test-install"]

    def test_it_installs_the_wheel_both_ways(self, commands):
        assert any(
            "[workflow]" in command for command in commands
        ), "the extra is never installed in CI"
