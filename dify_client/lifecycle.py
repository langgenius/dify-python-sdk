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

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, NoReturn

__all__ = ["Deployment", "PipelineDeployment", "Stage"]


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


def _stage(
    *, indeterminate: bool, imported: bool, published: bool, keyed: bool
) -> Stage:
    """The one-word summary, derived from the three facts and a key."""
    if indeterminate:
        return Stage.UNKNOWN
    if not imported:
        return Stage.NOT_IMPORTED
    if not published:
        return Stage.DRAFTED
    return Stage.RUNNABLE if keyed else Stage.PUBLISHED


def _reached(stage: Stage, *, imported: bool, published: bool, runnable: bool) -> bool:
    """Whether a deploy got as far as ``stage`` was asking for."""
    return {
        Stage.UNKNOWN: True,
        Stage.NOT_IMPORTED: True,
        Stage.DRAFTED: imported,
        Stage.PUBLISHED: published,
        Stage.RUNNABLE: runnable,
    }[stage]


def _refuse(
    *,
    reached: Stage,
    wanted: Stage,
    error: str,
    aftermath: str,
    payload: Mapping[str, Any],
) -> NoReturn:
    """Raise the one error both deploy results raise, worded by the caller."""
    from .exceptions import APIError

    detail = f": {error}" if error else ""
    msg = f"Deploy reached {reached} but {wanted} was wanted{detail}. {aftermath}"
    raise APIError(msg, 0, dict(payload))


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
    #: The import Dify held for confirmation, when it held one.
    import_id: str = ""
    #: Dify is holding this import over a DSL version difference. Not the same
    #: as a refusal: the import exists and ``apps.confirm(import_id)``
    #: completes it.
    needs_confirmation: bool = False
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
        return _stage(
            indeterminate=self.indeterminate,
            imported=self.imported,
            published=self.published,
            keyed=self.has_key,
        )

    @property
    def runnable(self) -> bool:
        """Published, with a key in hand."""
        return self.published and bool(self.api_key)

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def raise_for_stage(self, stage: Stage = Stage.RUNNABLE) -> Deployment:
        """Raise unless the deploy got this far, otherwise return self."""
        if (
            _reached(
                stage,
                imported=self.imported,
                published=self.published,
                runnable=self.runnable,
            )
            and not self.indeterminate
        ):
            return self
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
        elif self.needs_confirmation:
            aftermath = (
                "Dify is holding this import over a DSL version difference; "
                "nothing is built yet. Complete it with "
                f"apps.confirm({self.import_id!r})."
            )
        else:
            aftermath = "Nothing was created on Dify."
        _refuse(
            reached=self.stage,
            wanted=stage,
            error=self.error,
            aftermath=aftermath,
            payload=self.payload,
        )

    def __str__(self) -> str:
        return f"{self.stage} {self.app_id}".strip()


@dataclass(frozen=True)
class PipelineDeployment:
    """What a knowledge pipeline deploy came to.

    A pipeline is not an app, and it carries two ids rather than one: the
    ``pipeline_id`` addresses the graph and the ``dataset_id`` the knowledge
    base it fills. Deleting the dataset is what deletes the pipeline, so
    keeping them apart is what makes cleanup possible::

        result = management.pipelines.deploy(pipeline)
        management.pipelines.delete(result.dataset_id)
    """

    #: A draft exists on Dify.
    imported: bool = False
    #: A version is live, and a knowledge base will accept documents through it.
    published: bool = False
    pipeline_id: str = ""
    dataset_id: str = ""
    #: The import Dify held for confirmation, when it held one.
    import_id: str = ""
    #: Dify is holding this import over a DSL version difference. Not the same
    #: as a refusal: the import exists and ``confirm(import_id)`` completes it.
    needs_confirmation: bool = False
    imported_dsl_version: str = ""
    current_dsl_version: str = ""
    error: str = ""
    #: True when the outcome is genuinely unknown — the request went out and no
    #: answer came back. Not the same as a failure.
    indeterminate: bool = False
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def stage(self) -> Stage:
        # A pipeline has no key to mint: publishing is as far as it goes.
        return _stage(
            indeterminate=self.indeterminate,
            imported=self.imported,
            published=self.published,
            keyed=False,
        )

    def raise_for_stage(self, stage: Stage = Stage.PUBLISHED) -> PipelineDeployment:
        """Raise unless the deploy got this far, otherwise return self."""
        if (
            _reached(
                stage,
                imported=self.imported,
                published=self.published,
                # Nothing is minted for a pipeline, so published is as
                # runnable as it gets.
                runnable=self.published,
            )
            and not self.indeterminate
        ):
            return self
        if self.indeterminate:
            aftermath = (
                "Dify's answer never arrived, so whether the pipeline exists is "
                "unknown — check the workspace before retrying."
            )
        elif self.imported:
            aftermath = (
                f"Pipeline {self.pipeline_id} exists on Dify; delete its "
                f"knowledge base ({self.dataset_id}) or retry the publish."
            )
        elif self.needs_confirmation:
            aftermath = (
                f"Dify is holding this import over a DSL version difference "
                f"({self.imported_dsl_version or 'unknown'} against the server's "
                f"{self.current_dsl_version or 'unknown'}); nothing is built yet. "
                f"Complete it with pipelines.confirm({self.import_id!r})."
            )
        else:
            aftermath = "Nothing was created on Dify."
        _refuse(
            reached=self.stage,
            wanted=stage,
            error=self.error,
            aftermath=aftermath,
            payload=self.payload,
        )

    def __str__(self) -> str:
        return f"{self.stage} {self.pipeline_id}".strip()
