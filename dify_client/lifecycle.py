"""What happened to an app between your editor and a live run.

Dify keeps three things apart, and a name that blurs them hides which one a
call produced:

    a definition you wrote  →  a draft on Dify  →  a published version  →  runs

Importing a DSL writes a **draft**. The Service API runs the **published**
version. So importing and then running, without publishing in between, runs
whatever was published before — silently, with the old version's behaviour and
the old version's cost.

Three facts, not one ladder. An earlier version of this squashed them into a
single ``stage``, which made a failed import followed by a successful publish
report ``runnable``: the ladder let a later rung stand in for an earlier one.
They are independent, so they are separate:

* ``imported`` — a draft exists on Dify.
* ``published`` — a version is live, and the Service API will run it.
* ``api_key`` — there is a key to call it with.

``stage`` remains as a summary for printing, derived from those, and an
``UNKNOWN`` value exists for the case that has no good answer: the request went
out and nothing came back, so what Dify did is genuinely not known.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = ["Deployment", "Stage"]


class Stage(str, Enum):
    """A one-word summary of a deployment, for printing."""

    #: The request went out and no answer came back. What Dify did is unknown,
    #: so neither retrying nor cleaning up is obviously right.
    UNKNOWN = "unknown"
    #: Nothing reached Dify. The DSL was rejected before an app existed.
    NOT_IMPORTED = "not-imported"
    #: A draft exists. Nothing runs it yet.
    DRAFTED = "drafted"
    #: A version is published. The Service API will run it.
    PUBLISHED = "published"
    #: Published, and a Service-API key exists to call it with.
    RUNNABLE = "runnable"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Deployment:
    """What a deploy came to, and where it stopped if it stopped early.

    ::

        result = management.apps.deploy(workflow)
        if not result.runnable:
            print(result.stage, result.error)   # and decide what to do
    """

    #: A draft exists on Dify. False means nothing was created.
    imported: bool = False
    #: A version is live. Independent of ``imported`` only in that a deploy
    #: over an existing app can publish what was already there.
    published: bool = False
    app_id: str = ""
    app_mode: str = ""
    #: The version Dify published, when it reports one.
    version: str = ""
    #: Whether this deploy created the app, which is what makes deleting it safe.
    created: bool = False
    error: str = ""
    #: True when the outcome is genuinely unknown — the request went out and no
    #: answer came back. Not the same as a failure.
    indeterminate: bool = False
    warnings: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    #: Kept out of ``repr`` so printing a deploy result does not put a
    #: Service-API key in a log or a debugger pane.
    api_key: str = field(default="", repr=False)

    @property
    def stage(self) -> Stage:
        """The one-word summary, derived from the facts above."""
        if self.indeterminate:
            return Stage.UNKNOWN
        if not self.imported:
            return Stage.NOT_IMPORTED
        if not self.published:
            return Stage.DRAFTED
        return Stage.RUNNABLE if self.api_key else Stage.PUBLISHED

    @property
    def runnable(self) -> bool:
        """Published, with a key in hand."""
        return self.published and bool(self.api_key)

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def raise_for_stage(self, stage: Stage = Stage.RUNNABLE) -> Deployment:
        """Raise unless the deploy got this far, otherwise return self."""
        wanted = {
            Stage.UNKNOWN: True,
            Stage.NOT_IMPORTED: True,
            Stage.DRAFTED: self.imported,
            Stage.PUBLISHED: self.published,
            Stage.RUNNABLE: self.runnable,
        }[stage]
        if wanted and not self.indeterminate:
            return self
        from .exceptions import APIError

        detail = f": {self.error}" if self.error else ""
        if self.indeterminate:
            aftermath = (
                "Dify's answer never arrived, so whether the app exists is "
                "unknown — check the workspace before retrying."
            )
        elif self.imported:
            aftermath = (
                f"App {self.app_id} exists on Dify; delete it or retry the "
                "remaining steps."
            )
        else:
            aftermath = "Nothing was created on Dify."
        msg = f"Deploy reached {self.stage} but {stage} was wanted{detail}. {aftermath}"
        raise APIError(msg, 0, dict(self.payload))

    def __str__(self) -> str:
        return f"{self.stage} {self.app_id}".strip()
