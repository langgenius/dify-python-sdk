"""Dify's programmatic API at ``/openapi/v1``.

This is the surface Dify means third-party tools to use, and the one
``difyctl`` speaks. It authenticates with a scoped OAuth bearer (``dfoa_``)
rather than a console session, and it dispatches a run by app mode itself, so a
caller does not have to know whether an app is a workflow or a chatflow.

It is **off by default**, twice over:

* ``OPENAPI_ENABLED`` must be true, or every path 404s.
* The caller's ``client_id`` must appear in ``OPENAPI_KNOWN_CLIENT_IDS``
  (default: ``difyctl`` alone), or no token can be minted for it.

So this client is an opt-in upgrade, not a replacement: ``DifyManagement`` works
against any Dify. Use ``OpenApiClient.available()`` to find out which you have.

One gap to know about: there is no publish endpoint here. Importing a DSL
writes a draft, and publishing it is still a console operation.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, TypeAlias

import httpx

from .base_client import check_timeout
from .console import App, ImportResult, _import_result
from .exceptions import APIError, AuthenticationError, ValidationError
from .secrets import HOST_ENV, ApiKeyInput, resolve_api_key, resolve_host
from .streams import collect_run
from .version import user_agent

if TYPE_CHECKING:
    from .agent import Agent
    from .workflow import Workflow
    from .workflow.results import RunResult

    Deployable: TypeAlias = Workflow | Agent

#: Environment variable holding the OAuth bearer. The same one ``difyctl``
#: reads, and here it means the same thing.
# The name of an environment variable, not a credential.
OPENAPI_TOKEN_ENV = "DIFY_TOKEN"  # nosec B105

#: Where the surface is mounted.
OPENAPI_PATH = "/openapi/v1"

DEFAULT_HOST = "https://cloud.dify.ai"

#: Prefixes Dify gives the bearers minted for this surface.
TOKEN_PREFIXES = ("dfoa_", "dfoe_")


@dataclass(frozen=True)
class Workspace:
    """A workspace the caller can see."""

    id: str
    name: str
    role: str = ""
    current: bool = False


class OpenApiClient:
    """A client for ``/openapi/v1``.

    ::

        client = OpenApiClient()                 # reads DIFY_TOKEN and DIFY_HOST
        app = client.import_app(wf.to_yaml())
        result = client.run(app.app_id, {"name": "Dify"})

    ``workspace_id`` defaults to the caller's current workspace, resolved on
    first use.
    """

    def __init__(
        self,
        token: ApiKeyInput = None,
        base_url: str | None = None,
        workspace_id: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
    ):
        resolved_timeout = check_timeout(timeout, http_client)
        try:
            self._token = resolve_api_key(
                token,
                env_var=OPENAPI_TOKEN_ENV,
                label="Dify OpenAPI token",
                argument="token",
            )
        except ValueError as error:
            raise ValidationError(str(error)) from error

        self.base_url = resolve_host(base_url, DEFAULT_HOST, env_var=HOST_ENV)
        self._workspace_id = workspace_id
        from .resources.openapi import OpenApiApps

        #: The workspace's apps, and the steps from a definition to a run.
        self.apps = OpenApiApps(self)
        self.timeout = resolved_timeout
        self._client = http_client or httpx.Client(
            base_url=f"{self.base_url}{OPENAPI_PATH}",
            timeout=httpx.Timeout(resolved_timeout, connect=5.0),
        )

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> OpenApiClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def token(self) -> str:
        return self._token.reveal()

    def __repr__(self) -> str:
        return f"OpenApiClient(base_url={self.base_url!r}, token={self._token!r})"

    @staticmethod
    def available(base_url: str | None = None, *, timeout: float = 5.0) -> bool:
        """Whether this Dify serves ``/openapi/v1`` at all.

        Needs no token: the version endpoint is public. A false answer means
        the deployment has ``OPENAPI_ENABLED`` off, and ``DifyManagement`` is
        the way in.
        """
        host = resolve_host(base_url, DEFAULT_HOST, env_var=HOST_ENV)
        try:
            response = httpx.get(f"{host}{OPENAPI_PATH}/_version", timeout=timeout)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def version(self) -> dict[str, Any]:
        """The server's version and edition."""
        return self._get("/_version")

    # -- identity ----------------------------------------------------------

    def account(self) -> dict[str, Any]:
        """Who this token acts as.

        The reply nests the account under ``account`` and names the subject
        kind — an external-SSO bearer has no account at all — so it is returned
        whole rather than flattened into a guess.
        """
        return self._get("/account")

    @property
    def email(self) -> str | None:
        """The email this token acts as, if it acts as a person."""
        payload = self.account()
        subject = payload.get("subject_email")
        account = payload.get("account")
        if isinstance(account, dict):
            return account.get("email") or subject
        return subject

    def workspaces(self) -> list[Workspace]:
        """Workspaces the caller can see.

        ``/workspaces`` marks the current one; ``/account`` names a default
        instead, so the default is used when nothing is marked.
        """
        payload = self._get("/workspaces")
        items = payload.get("workspaces", [])
        default_id = payload.get("default_workspace_id")
        return [
            Workspace(
                id=item.get("id", ""),
                name=item.get("name", ""),
                role=item.get("role", ""),
                current=bool(item.get("current")) or item.get("id") == default_id,
            )
            for item in items
        ]

    @property
    def workspace_id(self) -> str:
        """The workspace this client works in, resolved on first use."""
        if self._workspace_id:
            return self._workspace_id
        spaces = self.workspaces()
        chosen = next((w for w in spaces if w.current), None)
        if chosen is None:
            default_id = self.account().get("default_workspace_id")
            chosen = next((w for w in spaces if w.id == default_id), None)
        chosen = chosen or (spaces[0] if spaces else None)
        if chosen is None:
            msg = (
                "This token can see no workspace, so there is nothing to work "
                "in. External-SSO bearers have no workspace membership by design."
            )
            raise ValidationError(msg)
        self._workspace_id = chosen.id
        return chosen.id

    # -- apps --------------------------------------------------------------

    def _apps(
        self, *, mode: str | None = None, page: int = 1, limit: int = 20
    ) -> list[App]:
        """List apps in the workspace."""
        params: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "page": page,
            "limit": limit,
        }
        if mode:
            params["mode"] = mode
        payload = self._get("/apps", params=params)
        return [
            App(
                id=item.get("id", ""),
                name=item.get("name", ""),
                mode=item.get("mode", ""),
                description=item.get("description", ""),
            )
            for item in payload.get("data", [])
        ]

    def _import_app(
        self,
        yaml_content: str,
        *,
        app_id: str | None = None,
        name: str | None = None,
    ) -> ImportResult:
        """Create an app from a DSL document, or overwrite an existing one.

        Overwriting works for workflow and chatflow apps; Dify's importer only
        creates new Agent apps.
        """
        body: dict[str, Any] = {"mode": "yaml-content", "yaml_content": yaml_content}
        if app_id:
            body["app_id"] = app_id
        if name:
            body["name"] = name
        return _import_result(
            self._request(
                "POST", f"/workspaces/{self.workspace_id}/apps/imports", json=body
            )
        )

    def _confirm_import(self, import_id: str) -> ImportResult:
        """Confirm an import Dify held back over a DSL version difference."""
        return _import_result(
            self._request(
                "POST",
                f"/workspaces/{self.workspace_id}/apps/imports/{import_id}:confirm",
            )
        )

    def _deploy(
        self,
        deployable: Deployable,
        *,
        app_id: str | None = None,
        name: str | None = None,
    ) -> ImportResult:
        """Push a code-defined Workflow or Agent to Dify."""
        result = self._import_app(
            deployable.to_yaml(),
            app_id=app_id,
            name=name or deployable.name,
        )
        return result.raise_for_status()

    def _export_app(self, app_id: str, *, include_secret: bool = False) -> str:
        """Export an app's DSL."""
        payload = self._get(
            f"/apps/{app_id}/dsl",
            params={"include_secret": str(include_secret).lower()},
        )
        data = payload.get("data")
        if not isinstance(data, str) or not data:
            msg = f"Dify returned no DSL for app {app_id}."
            raise ValidationError(msg)
        return data

    def _check_dependencies(self, app_id: str) -> dict[str, Any]:
        """Report plugins the app needs that this workspace does not have."""
        return self._get(f"/apps/{app_id}/dependencies:check")

    def _run(
        self,
        app_id: str,
        inputs: Mapping[str, Any] | None = None,
        *,
        query: str | None = None,
        conversation_id: str | None = None,
    ) -> RunResult:
        """Run an app and collect per-node results.

        Unlike the Service API, one path serves every app mode — Dify picks the
        handler — so a chatflow and a workflow are called the same way. A
        chatflow still needs ``query``, which becomes ``sys.query``.
        """
        body: dict[str, Any] = {"inputs": dict(inputs or {})}
        if query is not None:
            body["query"] = query
        if conversation_id:
            body["conversation_id"] = conversation_id

        with self._client.stream(
            "POST",
            f"/apps/{app_id}:run",
            json=body,
            headers=self._headers(),
        ) as response:
            if response.status_code >= 400:
                response.read()
                self._raise(response)
            return collect_run(response.iter_lines())

    def _upload_file(
        self,
        app_id: str,
        file: str | Path | BinaryIO,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload a file for an app's file inputs, returning its record.

        Args:
            app_id: The app the file belongs to.
            file: A path, or an already-open binary file.
            filename: Overrides the name Dify records. Required when ``file``
                is an object with no ``name``, such as a ``BytesIO``.
            content_type: Overrides the type guessed from the filename. Dify
                rejects an upload it cannot type.

        The ``id`` in the result is what a run's inputs reference:

        ```python
        uploaded = client.upload_file(app_id, "report.pdf")
        client.run(app_id, {"doc": {
            "transfer_method": "local_file",
            "upload_file_id": uploaded["id"],
            "type": "document",
        }})
        ```
        """
        if isinstance(file, (str, Path)):
            path = Path(file)
            name = filename or path.name
            content: Any = path.read_bytes()
        else:
            name = filename or getattr(file, "name", None)
            if not name:
                msg = (
                    "This file has no name to record. Pass filename=... — Dify "
                    "types the upload by its extension and rejects one it "
                    "cannot type."
                )
                raise ValidationError(msg)
            name = Path(str(name)).name
            content = file.read()

        guessed = content_type or mimetypes.guess_type(name)[0]
        if not guessed:
            msg = (
                f"Cannot tell what kind of file {name!r} is. Pass "
                "content_type=... — Dify answers an untyped upload with 415."
            )
            raise ValidationError(msg)

        # Multipart sets its own Content-Type, boundary and all.
        headers = self._headers()
        headers.pop("Content-Type", None)
        response = self._client.post(
            f"/apps/{app_id}/files",
            files={"file": (name, content, guessed)},
            headers=headers,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            self._raise(response, body)
        return body if isinstance(body, dict) else {}

    # -- internals ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token.reveal()}",
            "Content-Type": "application/json",
            "User-Agent": user_agent(self._client.headers.get("User-Agent")),
        }

    def _get(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._client.request(
            method,
            path,
            json=dict(json) if json is not None else None,
            params=dict(params) if params is not None else None,
            headers=self._headers(),
        )
        try:
            body = response.json()
        except ValueError:
            body = {}

        # An import that Dify declines still answers with a usable status; let
        # the caller read it rather than raising over it.
        if response.status_code >= 400 and not isinstance(body.get("status"), str):
            self._raise(response, body)
        return body if isinstance(body, dict) else {}

    def _raise(
        self, response: httpx.Response, body: Mapping[str, Any] | None = None
    ) -> None:
        if body is None:
            try:
                body = response.json()
            except ValueError:
                body = {}
        if response.status_code == 401:
            msg = (
                "The OpenAPI token was rejected. These expire; mint a fresh one "
                "with DifyManagement.mint_openapi_token() and set "
                f"{OPENAPI_TOKEN_ENV}."
            )
            raise AuthenticationError(msg, status_code=401, headers=response.headers)
        if response.status_code == 404 and not body:
            msg = (
                f"{OPENAPI_PATH} is not served by {self.base_url}. It is off by "
                "default — the deployment needs OPENAPI_ENABLED=true — and "
                "DifyManagement works without it."
            )
            raise APIError(
                msg, status_code=404, response=dict(body), headers=response.headers
            )
        message = (
            body.get("message") or body.get("error") or f"HTTP {response.status_code}"
        )
        raise APIError(
            str(message),
            status_code=response.status_code,
            response=dict(body),
            headers=response.headers,
        )
