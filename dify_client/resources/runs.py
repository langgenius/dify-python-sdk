"""Workflow runs: starting one, watching it, and asking about it later."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..paging import by_page, by_page_async
from ..results import AsyncPage, Page, WorkflowRun, form_tokens
from ..streams import AsyncWorkflowRunStream, WorkflowRunStream
from ..usage import Usage
from ._base import Resource


def _run_from_blocking(payload: Mapping[str, Any]) -> WorkflowRun:
    """Build a run from a blocking response, which nests it under ``data``.

    Blocking mode reports one workflow-wide token count and no per-node detail
    — the price and the node breakdown exist only on the stream. That figure is
    kept as ``reported_usage`` rather than invented into a nameless node
    execution, which is what an earlier version did: the fake execution then
    showed up in ``executions`` and in anything iterating it.
    """
    data = payload.get("data")
    data = data if isinstance(data, dict) else dict(payload)

    reported = None
    if data.get("total_tokens") or data.get("total_price"):
        reported = Usage.from_metadata(data, data.get("elapsed_time"))

    return WorkflowRun(
        status=str(data.get("status") or "unknown"),
        outputs=dict(data.get("outputs") or {}),
        error=str(data.get("error") or ""),
        run_id=str(data.get("id") or payload.get("workflow_run_id") or ""),
        task_id=str(payload.get("task_id") or ""),
        # A run that pauses answers blocking too, with the tokens to resume it
        # under `data.reasons`. Without them the caller has a paused run and
        # nothing to submit a form against.
        pending_forms=form_tokens(payload),
        reported_usage=reported,
    )


class WorkflowRuns(Resource):
    """Runs of this app's workflow.

    ``create`` waits for the run and returns it. ``stream`` hands back the run
    as it happens. ``retrieve`` reads one back afterwards, and ``stop`` ends one
    that is still going.
    """

    def create(
        self,
        inputs: Mapping[str, Any] | None = None,
        *,
        user: str | None = None,
        files: Any = None,
        workflow_id: str | None = None,
    ) -> WorkflowRun:
        """Run the workflow and wait for it.

        Returns a :class:`~dify_client.results.WorkflowRun`, whose ``outputs``
        hold the result and whose ``run_id`` reads it back later.
        """
        body: dict[str, Any] = {
            "inputs": dict(inputs or {}),
            "response_mode": "blocking",
            "user": self._who(user),
        }
        if files is not None:
            body["files"] = files
        # A workflow_id names a published version other than the current one,
        # which is how a caller pins a run to a version they have tested.
        path = f"/workflows/{workflow_id}/run" if workflow_id else "/workflows/run"
        response = self._client._send_request("POST", path, body)
        return _run_from_blocking(response.json())

    def stream(
        self,
        inputs: Mapping[str, Any] | None = None,
        *,
        user: str | None = None,
        files: Any = None,
        workflow_id: str | None = None,
        raise_on_error: bool = True,
    ) -> WorkflowRunStream:
        """Run the workflow and watch it happen.

        Use it as a context manager so the connection is released even when the
        loop stops early::

            with app.workflows.runs.stream(inputs) as stream:
                for event in stream:
                    ...
                run = stream.get_final_run()

        Closing the stream stops watching, not the run — see :meth:`stop`.
        """
        body: dict[str, Any] = {
            "inputs": dict(inputs or {}),
            "response_mode": "streaming",
            "user": self._who(user),
        }
        if files is not None:
            body["files"] = files
        path = f"/workflows/{workflow_id}/run" if workflow_id else "/workflows/run"
        response = self._client._send_request("POST", path, body, stream=True)
        return WorkflowRunStream(response, raise_on_error=raise_on_error)

    def retrieve(self, run_id: str) -> WorkflowRun:
        """Read a finished run back by its id."""
        response = self._client._send_request("GET", f"/workflows/run/{run_id}")
        return _run_from_blocking(response.json())

    def events(
        self, run_id: str, *, user: str | None = None, resume_paused: bool = False
    ) -> WorkflowRunStream:
        """Reopen a run's stream — after a pause, or a dropped connection.

        A dropped connection says nothing about the run; this picks it back up.
        A run that already finished emits one event and closes.

        Args:
            run_id: From the run you are following.
            user: The identifier the run was started with.
            resume_paused: Hold the stream open across a pause rather than
                ending at it.
        """
        params = {
            "user": self._who(user),
            "include_state_snapshot": "false",
            "continue_on_pause": str(resume_paused).lower(),
        }
        response = self._client._send_request(
            "GET", f"/workflow/{run_id}/events", params=params, stream=True
        )
        return WorkflowRunStream(response)

    def stop(self, run: WorkflowRun | str, *, user: str | None = None) -> None:
        """Stop a run that is still going.

        Takes the run itself, or the ``task_id`` Dify's endpoint names — which
        is not the ``run_id``, and is why the run carries both.
        """
        task_id = run if isinstance(run, str) else run.task_id
        if not task_id:
            from ..exceptions import ValidationError

            msg = (
                "This run carries no task_id, so there is nothing to stop. "
                "Only a streaming run reports one."
            )
            raise ValidationError(msg)
        self._client._send_request(
            "POST", f"/workflows/tasks/{task_id}/stop", {"user": self._who(user)}
        )

    def logs(
        self,
        *,
        page: int = 1,
        limit: int = 20,
        status: str | None = None,
        keyword: str | None = None,
        **filters: Any,
    ) -> Page[dict[str, Any]]:
        """The app's run history, as Dify records it.

        Runs Dify has already finished with, including ones this client never
        watched. ``retrieve(run_id)`` reads one back in full.
        """

        def fetch(number: int) -> dict[str, Any]:
            return self._client._send_request(
                "GET",
                "/workflows/logs",
                params={
                    "page": number,
                    "limit": limit,
                    "status": status,
                    "keyword": keyword,
                    **filters,
                },
            ).json()

        return by_page(fetch(page), dict, fetch, page)


class AsyncWorkflowRuns(Resource):
    """The async counterpart of :class:`WorkflowRuns`."""

    async def create(
        self,
        inputs: Mapping[str, Any] | None = None,
        *,
        user: str | None = None,
        files: Any = None,
        workflow_id: str | None = None,
    ) -> WorkflowRun:
        body: dict[str, Any] = {
            "inputs": dict(inputs or {}),
            "response_mode": "blocking",
            "user": self._who(user),
        }
        if files is not None:
            body["files"] = files
        path = f"/workflows/{workflow_id}/run" if workflow_id else "/workflows/run"
        response = await self._client._send_request("POST", path, body)
        return _run_from_blocking(response.json())

    async def stream(
        self,
        inputs: Mapping[str, Any] | None = None,
        *,
        user: str | None = None,
        files: Any = None,
        workflow_id: str | None = None,
        raise_on_error: bool = True,
    ) -> AsyncWorkflowRunStream:
        body: dict[str, Any] = {
            "inputs": dict(inputs or {}),
            "response_mode": "streaming",
            "user": self._who(user),
        }
        if files is not None:
            body["files"] = files
        path = f"/workflows/{workflow_id}/run" if workflow_id else "/workflows/run"
        response = await self._client._send_request("POST", path, body, stream=True)
        return AsyncWorkflowRunStream(response, raise_on_error=raise_on_error)

    async def retrieve(self, run_id: str) -> WorkflowRun:
        response = await self._client._send_request("GET", f"/workflows/run/{run_id}")
        return _run_from_blocking(response.json())

    async def events(
        self, run_id: str, *, user: str | None = None, resume_paused: bool = False
    ) -> AsyncWorkflowRunStream:
        params = {
            "user": self._who(user),
            "include_state_snapshot": "false",
            "continue_on_pause": str(resume_paused).lower(),
        }
        response = await self._client._send_request(
            "GET", f"/workflow/{run_id}/events", params=params, stream=True
        )
        return AsyncWorkflowRunStream(response)

    async def stop(self, run: WorkflowRun | str, *, user: str | None = None) -> None:
        task_id = run if isinstance(run, str) else run.task_id
        if not task_id:
            from ..exceptions import ValidationError

            msg = "This run carries no task_id, so there is nothing to stop."
            raise ValidationError(msg)
        await self._client._send_request(
            "POST", f"/workflows/tasks/{task_id}/stop", {"user": self._who(user)}
        )

    async def logs(
        self,
        *,
        page: int = 1,
        limit: int = 20,
        status: str | None = None,
        keyword: str | None = None,
        **filters: Any,
    ) -> AsyncPage[dict[str, Any]]:
        """This app's run history. ``page.all()`` walks every page."""

        async def fetch(number: int) -> dict[str, Any]:
            response = await self._client._send_request(
                "GET",
                "/workflows/logs",
                params={
                    "page": number,
                    "limit": limit,
                    "status": status,
                    "keyword": keyword,
                    **filters,
                },
            )
            return dict(response.json())

        return by_page_async(await fetch(page), dict, fetch, page)
