"""Human-input forms: the runs that stop and wait for a person."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, List

from ..results import Message, WorkflowRun
from ._base import Resource


@dataclass(frozen=True)
class Form:
    """A paused run's form, as it should be shown to whoever fills it in."""

    token: str
    content: str = ""
    inputs: List[dict[str, Any]] = field(default_factory=list)
    actions: List[dict[str, Any]] = field(default_factory=list)
    defaults: dict[str, str] = field(default_factory=dict)
    expires_at: int | None = None


def _token(source: Form | WorkflowRun | Message | str) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, Form):
        return source.token
    pending = getattr(source, "pending_forms", [])
    if pending:
        return pending[0]

    from ..exceptions import ValidationError

    if getattr(source, "paused", False):
        # Paused, but with nothing to submit against. Dify omits the token for
        # a form it means to be answered in its own UI, and saying "not
        # waiting on a form" of a run that plainly is sends the caller looking
        # in the wrong place.
        nodes = ", ".join(getattr(source, "paused_nodes", []) or []) or "a node"
        msg = (
            f"This run is paused at {nodes}, but Dify reported no form token "
            "for it — the form is one it expects to be answered in its own UI "
            "(display_in_ui), and the API has nothing to submit against."
        )
    else:
        msg = "This run is not waiting on a form. `run.paused` says whether it is."
    raise ValidationError(msg)


class Forms(Resource):
    """The forms a paused run is waiting on.

    Waiting is not failing: a run that reaches a human-input node stops, its
    stream ends, and it resumes when the form comes back. ``run.paused`` says
    so, and ``run.pending_forms`` carries the token.
    """

    def retrieve(self, source: Form | WorkflowRun | Message | str) -> Form:
        """Fetch a form, by a paused run or by its token."""
        token = _token(source)
        payload = self._client._send_request("GET", f"/form/human_input/{token}").json()
        return Form(
            token=token,
            content=str(payload.get("form_content") or ""),
            inputs=list(payload.get("inputs") or []),
            actions=list(payload.get("user_actions") or []),
            defaults=dict(payload.get("resolved_default_values") or {}),
            expires_at=payload.get("expiration_time"),
        )

    def submit(
        self,
        source: Form | WorkflowRun | Message | str,
        inputs: Mapping[str, Any],
        *,
        action: str,
        user: str | None = None,
    ) -> None:
        """Answer the form, which resumes the run.

        Forms are one-shot: the first submission wins and a second answers 412.
        Follow the resumed run with ``app.workflows.runs.events(run.run_id)``.
        """
        self._client._send_request(
            "POST",
            f"/form/human_input/{_token(source)}",
            {
                "inputs": dict(inputs),
                "action": action,
                "user": self._who(user),
            },
        )


class AsyncForms(Resource):
    """The async counterpart of :class:`Forms`."""

    async def retrieve(self, source: Form | WorkflowRun | Message | str) -> Form:
        """Fetch a form, by a paused run or by its token."""
        token = _token(source)
        payload = (
            await self._client._send_request("GET", f"/form/human_input/{token}")
        ).json()
        return Form(
            token=token,
            content=str(payload.get("form_content") or ""),
            inputs=list(payload.get("inputs") or []),
            actions=list(payload.get("user_actions") or []),
            defaults=dict(payload.get("resolved_default_values") or {}),
            expires_at=payload.get("expiration_time"),
        )

    async def submit(
        self,
        source: Form | WorkflowRun | Message | str,
        inputs: Mapping[str, Any],
        *,
        action: str,
        user: str | None = None,
    ) -> None:
        """Answer the form, which resumes the run.

        Forms are one-shot: the first submission wins and a second answers 412.
        Follow the resumed run with ``app.workflows.runs.events(run.run_id)``.
        """
        await self._client._send_request(
            "POST",
            f"/form/human_input/{_token(source)}",
            {
                "inputs": dict(inputs),
                "action": action,
                "user": self._who(user),
            },
        )
