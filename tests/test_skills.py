"""Agent skills: packaging a SKILL.md, installing it, binding it to an Agent."""

import io
import zipfile

import httpx
import pytest
import yaml

from dify_client import Agent, DifyManagement, Skill, SkillError
from dify_client.console import CONSOLE_TOKEN_ENV, CONSOLE_URL_ENV
from dify_client.exceptions import ValidationError
from dify_client.skills import ENTRY, parse_frontmatter

TOKEN = "ey.FAKE.CONSOLE.TOKEN"

SKILL_MD = """---
name: paging-policy
description: When to page on-call and when to queue.
---

# Paging policy

Queue when in doubt.
"""


@pytest.fixture(autouse=True)
def _no_ambient(monkeypatch):
    monkeypatch.delenv(CONSOLE_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CONSOLE_URL_ENV, raising=False)


@pytest.fixture
def skill_dir(tmp_path):
    root = tmp_path / "paging-policy"
    (root / "references").mkdir(parents=True)
    (root / ENTRY).write_text(SKILL_MD, encoding="utf-8")
    (root / "references" / "examples.md").write_text("# Examples\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
    return root


class TestFrontmatter:
    def test_it_reads_name_and_description(self):
        assert parse_frontmatter(SKILL_MD) == (
            "paging-policy",
            "When to page on-call and when to queue.",
        )

    def test_an_empty_file_is_rejected(self):
        with pytest.raises(SkillError, match="empty"):
            parse_frontmatter("   ")

    def test_missing_frontmatter_shows_the_shape(self):
        with pytest.raises(SkillError, match="name: my-skill"):
            parse_frontmatter("# just markdown\n")

    def test_an_unclosed_block_is_rejected(self):
        with pytest.raises(SkillError, match="not closed"):
            parse_frontmatter("---\nname: x\n")

    def test_invalid_yaml_is_reported(self):
        with pytest.raises(SkillError, match="not valid YAML"):
            parse_frontmatter("---\nname: [unclosed\n---\n")


class TestPackaging:
    def test_a_directory_becomes_a_package(self, skill_dir):
        skill = Skill.from_directory(skill_dir)
        assert skill.name == "paging-policy"
        assert set(skill.files) == {ENTRY, "references/examples.md"}

    def test_junk_directories_are_left_out(self, skill_dir):
        assert not any(
            "__pycache__" in p for p in Skill.from_directory(skill_dir).files
        )

    def test_a_directory_without_skill_md_says_so(self, tmp_path):
        with pytest.raises(SkillError, match=ENTRY):
            Skill.from_directory(tmp_path)

    def test_the_archive_contains_the_files(self, skill_dir):
        with zipfile.ZipFile(
            io.BytesIO(Skill.from_directory(skill_dir).archive())
        ) as z:
            assert sorted(z.namelist()) == [ENTRY, "references/examples.md"]
            assert b"Paging policy" in z.read(ENTRY)

    def test_packaging_is_deterministic(self, skill_dir):
        """A rebuilt package hashes the same when nothing changed."""
        assert (
            Skill.from_directory(skill_dir).digest
            == Skill.from_directory(skill_dir).digest
        )

    def test_changing_a_file_changes_the_digest(self, skill_dir):
        before = Skill.from_directory(skill_dir).digest
        (skill_dir / "references" / "examples.md").write_text(
            "# Changed\n", encoding="utf-8"
        )
        assert Skill.from_directory(skill_dir).digest != before

    def test_it_writes_a_zip(self, skill_dir, tmp_path):
        path = Skill.from_directory(skill_dir).write(tmp_path / "s.zip")
        assert zipfile.is_zipfile(path)

    def test_a_skill_can_be_built_from_a_string(self):
        skill = Skill.from_markdown(SKILL_MD, {"notes.md": b"# Notes"})
        assert set(skill.files) == {ENTRY, "notes.md"}


class TestValidation:
    """What Dify checks, checked before a round trip finds out."""

    @pytest.mark.parametrize(
        "name", ["Paging Policy", "paging_policy", "Paging", "paging--"]
    )
    def test_a_name_outside_dify_s_pattern_is_rejected(self, name):
        with pytest.raises(SkillError, match="lower-case words"):
            Skill.from_markdown(f"---\nname: {name}\ndescription: d\n---\n").validate()

    @pytest.mark.parametrize("name", ["paging-policy", "triage", "a1-b2-c3"])
    def test_a_valid_name_passes(self, name):
        Skill.from_markdown(f"---\nname: {name}\ndescription: d\n---\n").validate()

    def test_a_missing_description_explains_why_it_matters(self):
        with pytest.raises(SkillError, match="when to reach for the skill"):
            Skill.from_markdown("---\nname: ok\n---\n").validate()

    def test_an_over_long_description_is_rejected(self):
        long = "x" * 1025
        with pytest.raises(SkillError, match="1024"):
            Skill.from_markdown(f"---\nname: ok\ndescription: {long}\n---\n").validate()

    def test_an_over_long_name_is_rejected(self):
        name = "-".join(["ab"] * 30)
        with pytest.raises(SkillError, match="64"):
            Skill.from_markdown(f"---\nname: {name}\ndescription: d\n---\n").validate()


class TestBinding:
    def test_a_skill_is_bound_by_name(self):
        agent = Agent.create("a", skills=["paging-policy"])
        assert agent.skills == ["paging-policy"]

    def test_bindings_reach_the_exported_package(self):
        agent = Agent.create("a", skills=["paging-policy"])
        package = yaml.safe_load(agent.to_yaml())["agent_packages"]["agent_1"]
        assert package["workspace_skills"] == [
            {
                "name": "paging-policy",
                "display_name": "",
                "description": "",
                "priority": 0,
            }
        ]

    def test_priority_defaults_to_the_next_slot(self):
        agent = Agent.create("a", skills=["first", "second"])
        assert [s["priority"] for s in agent.workspace_skills] == [0, 1]

    def test_priority_can_be_chosen(self):
        agent = Agent.create("a")
        agent.use_skill("late", priority=9)
        assert agent.workspace_skills[0]["priority"] == 9

    def test_binding_the_same_name_twice_replaces_it(self):
        agent = Agent.create("a", skills=["paging-policy"])
        agent.use_skill("paging-policy", priority=3)
        assert len(agent.workspace_skills) == 1
        assert agent.workspace_skills[0]["priority"] == 3

    def test_an_agent_without_skills_omits_the_field(self):
        """Older Dify releases reject an unknown key outright."""
        assert (
            "workspace_skills"
            not in Agent.create("a").to_dict()["agent_packages"]["agent_1"]
        )


class TestThroughTheConsole:
    def client(self, handler) -> DifyManagement:
        http = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://dify.test/console/api",
        )
        return DifyManagement(
            token=TOKEN, base_url="https://dify.test", http_client=http
        )

    def test_importing_uploads_the_archive_then_publishes(self):
        """An imported skill is a draft; an Agent binds to a published version."""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.replace("/console/api", "")
            calls.append((request.method, path))
            if path.endswith("/skills/import"):
                return httpx.Response(
                    201,
                    json={"id": "sk-1", "name": "paging-policy", "description": "d"},
                )
            if path.endswith("/publish"):
                return httpx.Response(200, json={"version_number": 1})
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "sk-1",
                            "name": "paging-policy",
                            "description": "d",
                            "latest_published_version_number": 1,
                        }
                    ]
                },
            )

        skill = self.client(handler).skills.create(Skill.from_markdown(SKILL_MD))
        assert skill.published_version == 1
        assert ("POST", "/workspaces/current/skills/import") in calls
        assert ("POST", "/workspaces/current/skills/sk-1/publish") in calls

    def test_publishing_can_be_skipped(self):
        def handler(request):
            return httpx.Response(201, json={"id": "sk-1", "name": "paging-policy"})

        skill = self.client(handler).skills.create(
            Skill.from_markdown(SKILL_MD), publish=False
        )
        assert skill.published is False

    def test_an_invalid_package_never_reaches_the_server(self):
        def handler(request):
            raise AssertionError("no request should have been made")

        with pytest.raises(SkillError):
            self.client(handler).skills.create(
                Skill.from_markdown("---\nname: Bad Name\ndescription: d\n---\n")
            )

    def test_deleting_sends_the_confirmation_dify_asks_for(self):
        import json as _json

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(_json.loads(request.content))
            return httpx.Response(204)

        from dify_client import WorkspaceSkill

        self.client(handler).skills.delete(
            WorkspaceSkill(id="sk-1", name="paging-policy")
        )
        assert seen == {"confirmation_name": "paging-policy"}

    def test_deleting_by_id_needs_the_name(self):
        def handler(request):
            raise AssertionError("no request should have been made")

        with pytest.raises(ValidationError, match="confirmation"):
            self.client(handler).skills.delete("sk-1")

    def test_an_unknown_skill_name_is_reported(self):
        def handler(request):
            return httpx.Response(200, json={"data": []})

        with pytest.raises(ValidationError, match="no skill named"):
            self.client(handler).skills.retrieve("nope")
