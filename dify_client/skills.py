"""Dify Agent skills: a SKILL.md and its files, packaged for upload.

A Dify skill is a zip archive containing ``SKILL.md`` whose YAML frontmatter
names it and says when to use it — the same shape a Claude Code skill has, so a
directory written for one can usually serve as the other.

Two kinds exist, and they behave differently:

* a **workspace skill** lives in the workspace and Agents bind to it by name;
  this is what travels in an exported Agent, as ``workspace_skills``
* a **config skill** is an archive attached to one Agent; an export records
  only that it was there, because the bytes cannot travel in a DSL

Building and validating a package is offline work. Uploading is not.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: What Dify accepts as a skill name.
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

#: The file Dify looks for to find the skill's root.
ENTRY = "SKILL.md"

#: Directories never worth shipping inside a skill.
_SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".DS_Store",
}


class SkillError(Exception):
    """Raised when a skill package is not one Dify would accept."""


@dataclass
class Skill:
    """A skill package, in memory.

    ``Skill.from_directory()`` reads a folder holding ``SKILL.md`` plus any
    references, scripts or assets beside it::

        skill = Skill.from_directory("skills/paging-policy")
        console.import_skill(skill)
    """

    name: str
    description: str
    files: dict[str, bytes]

    @classmethod
    def from_directory(cls, path: str | Path) -> Skill:
        """Read a skill from a directory containing ``SKILL.md``."""
        root = Path(path)
        entry = root / ENTRY
        if not entry.is_file():
            msg = (
                f"{root} has no {ENTRY}. A Dify skill is a folder whose "
                f"{ENTRY} carries the frontmatter naming it."
            )
            raise SkillError(msg)

        files: dict[str, bytes] = {}
        for item in sorted(root.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(root)
            if any(part in _SKIP_DIRS for part in relative.parts):
                continue
            files[relative.as_posix()] = item.read_bytes()

        name, description = parse_frontmatter(files[ENTRY].decode("utf-8"))
        return cls(name=name, description=description, files=files)

    @classmethod
    def from_markdown(
        cls, markdown: str, files: dict[str, bytes] | None = None
    ) -> Skill:
        """Build a skill from a ``SKILL.md`` string and optional extra files."""
        name, description = parse_frontmatter(markdown)
        contents = {ENTRY: markdown.encode("utf-8")}
        contents.update(files or {})
        return cls(name=name, description=description, files=contents)

    # -- packaging ---------------------------------------------------------

    def validate(self) -> None:
        """Check what Dify checks, before a round trip finds out."""
        if ENTRY not in self.files:
            msg = f"A skill package must contain {ENTRY}."
            raise SkillError(msg)
        if not NAME_PATTERN.match(self.name or ""):
            msg = (
                f"name={self.name!r} is not a Dify skill name. It must be "
                "lower-case words joined by hyphens, such as 'paging-policy'."
            )
            raise SkillError(msg)
        if len(self.name) > MAX_NAME_LENGTH:
            msg = f"name is {len(self.name)} characters; Dify allows {MAX_NAME_LENGTH}."
            raise SkillError(msg)
        if not self.description:
            msg = (
                "A skill needs a description in its frontmatter: it is what "
                "tells the Agent when to reach for the skill."
            )
            raise SkillError(msg)
        if len(self.description) > MAX_DESCRIPTION_LENGTH:
            msg = (
                f"description is {len(self.description)} characters; Dify "
                f"allows {MAX_DESCRIPTION_LENGTH}."
            )
            raise SkillError(msg)

    def archive(self) -> bytes:
        """The zip Dify accepts, built deterministically."""
        self.validate()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.files):
                # A fixed timestamp keeps the bytes stable across builds, so a
                # rebuilt package hashes the same when nothing changed.
                info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, self.files[path])
        return buffer.getvalue()

    @property
    def digest(self) -> str:
        """A sha256 of the packaged bytes, for telling versions apart."""
        return hashlib.sha256(self.archive()).hexdigest()

    def write(self, path: str | Path) -> Path:
        """Write the package to a ``.zip``."""
        target = Path(path)
        target.write_bytes(self.archive())
        return target

    def __repr__(self) -> str:
        return f"Skill({self.name!r}, {len(self.files)} files)"


@dataclass(frozen=True)
class WorkspaceSkill:
    """A skill installed in a workspace, as the console reports it."""

    id: str
    name: str
    description: str = ""
    display_name: str = ""
    published_version: int | None = None
    reference_count: int = 0

    @property
    def published(self) -> bool:
        """Whether a published version exists for Agents to bind to."""
        return self.published_version is not None

    def __repr__(self) -> str:
        state = f"v{self.published_version}" if self.published else "unpublished"
        return f"WorkspaceSkill({self.name!r}, {state})"


def parse_frontmatter(markdown: str) -> tuple[str, str]:
    """Read ``name`` and ``description`` out of a ``SKILL.md``.

    Matches Dify's own reading: the block between the first two ``---`` lines,
    parsed as YAML, with everything else ignored.
    """
    if not markdown.strip():
        msg = f"{ENTRY} is empty."
        raise SkillError(msg)
    if not markdown.startswith("---"):
        msg = (
            f"{ENTRY} has no frontmatter. Open it with a --- block naming the "
            "skill:\n\n---\nname: my-skill\ndescription: When to use it.\n---"
        )
        raise SkillError(msg)

    parts = markdown.split("---", 2)
    if len(parts) < 3:
        msg = f"{ENTRY} frontmatter is not closed by a second --- line."
        raise SkillError(msg)
    try:
        loaded = yaml.safe_load(parts[1])
    except yaml.YAMLError as error:
        msg = f"{ENTRY} frontmatter is not valid YAML: {error}"
        raise SkillError(msg) from error

    front: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
    return (
        str(front.get("name") or "").strip(),
        str(front.get("description") or "").strip(),
    )
