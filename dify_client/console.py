"""Deploying workflows to Dify through its console API.

The Service API (`/v1`) runs apps; it cannot create or update them. Creating
one means the console API, which authenticates as an *account* rather than as
an app. There is no deploy-scoped credential in Dify, so a console token is a
full-privilege credential — treat it like the password it stands in for, and
keep it out of anything an app-level key would do instead.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias

import httpx

from .base_client import check_timeout
from .catalog import ModelProvider, providers_from
from .exceptions import APIError, AuthenticationError, ValidationError
from .paging import by_page
from .results import Page
from .secrets import HOST_ENV, ApiKeyInput, resolve_api_key, resolve_host
from .skills import Skill, WorkspaceSkill
from .tools import ToolCatalog, parse_provider
from .version import user_agent

if TYPE_CHECKING:
    from .agent import Agent
    from .workflow import Workflow

    #: Anything this SDK can push to Dify as an app: a Workflow or an Agent.
    Deployable: TypeAlias = Workflow | Agent

#: Environment variable holding a console session token.
#:
#: Deliberately *not* ``DIFY_CONSOLE_TOKEN``: that is what ``difyctl`` reads, and it
#: holds a ``dfoa_`` OAuth bearer for the ``/openapi/v1`` surface — a different
#: credential for a different API. The host variable is shared, because that
#: one does mean the same thing in both.
# The name of an environment variable, not a credential.
CONSOLE_TOKEN_ENV = "DIFY_CONSOLE_TOKEN"  # nosec B105

#: Environment variable holding the Dify host, e.g. ``https://cloud.dify.ai``.
#: Shared with ``difyctl``, which reads the same one.
CONSOLE_URL_ENV = HOST_ENV

#: Prefixes Dify gives the OAuth bearers minted for ``/openapi/v1``.
OPENAPI_TOKEN_PREFIXES = ("dfoa_", "dfoe_")

#: Where the device flow that mints those bearers lives.
_OPENAPI_DEVICE_PATH = "/openapi/v1/oauth/device"

DEFAULT_CONSOLE_URL = "https://cloud.dify.ai"

#: Header carrying the CSRF token the console API checks on every request.
CSRF_HEADER = "X-CSRF-Token"

#: Environment variable holding that CSRF token. Dify 1.17 and later reject
#: a console write that arrives without one, so an access token alone can
#: read but not write. ``login()`` collects both.
CONSOLE_CSRF_ENV = "DIFY_CONSOLE_CSRF_TOKEN"

#: App modes whose runs are messages rather than bare workflow runs.
_CHAT_APP_MODES = frozenset({"chat", "advanced-chat", "agent-chat"})

#: Import outcomes Dify reports as usable.
_OK_STATUSES = {"completed", "completed-with-warnings"}


@dataclass
class ImportResult:
    """What Dify made of an imported DSL document."""

    id: str
    status: str
    app_id: str | None = None
    app_mode: str | None = None
    imported_dsl_version: str = ""
    current_dsl_version: str = ""
    error: str = ""
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status in _OK_STATUSES

    @property
    def needs_confirmation(self) -> bool:
        """A major DSL version gap: Dify wants the import confirmed."""
        return self.status == "pending"

    def raise_for_status(self) -> ImportResult:
        if self.needs_confirmation:
            msg = (
                f"Dify wants this import confirmed: the document is DSL "
                f"{self.imported_dsl_version} and the server is on "
                f"{self.current_dsl_version}. Call confirm_import(result.id) "
                "once you are satisfied the difference is safe."
            )
            raise ValidationError(msg)
        if not self.succeeded:
            msg = f"Import {self.status}: {self.error or 'no error detail'}"
            raise ValidationError(msg)
        return self


@dataclass
class ApiKey:
    """An app's Service-API key.

    Dify returns the full token only when a key is created; listing keys shows
    a masked form. Create one, keep it, and reuse it — there is also a cap on
    how many an app may have.
    """

    id: str
    token: str
    type: str = "app"
    created_at: int | None = None

    @property
    def is_masked(self) -> bool:
        """Whether this came from a listing, and so is not usable."""
        return "*" in self.token


def _app_summary(payload: Mapping[str, Any]) -> App:
    return App(
        id=str(payload.get("id", "")),
        name=str(payload.get("name", "")),
        mode=str(payload.get("mode") or ""),
        description=str(payload.get("description") or ""),
    )


@dataclass(frozen=True)
class App:
    """An app in the workspace, as Dify lists it.

    Shared with :mod:`dify_client.openapi`, which reports the same shape.
    """

    id: str
    name: str
    mode: str = ""
    description: str = ""


@dataclass(frozen=True)
class Trigger:
    """A way a published workflow starts by itself."""

    id: str
    type: str
    title: str
    node_id: str
    status: str
    provider_name: str = ""

    @property
    def enabled(self) -> bool:
        return self.status == "enabled"


@dataclass(frozen=True)
class WebhookTrigger:
    """The endpoint Dify minted for a webhook trigger node."""

    id: str
    webhook_id: str
    url: str
    debug_url: str
    node_id: str


@dataclass(frozen=True)
class AgentSummary:
    """An Agent as the roster reports it.

    ``app_id`` is what every app-shaped call wants — export, delete, provision —
    while ``id`` addresses the Agent itself on the roster.
    """

    id: str
    app_id: str
    name: str
    role: str = ""
    description: str = ""
    published: bool = False

    def __repr__(self) -> str:
        return f"AgentSummary({self.name!r}, role={self.role!r})"


class DifyManagement:
    """A client for the Dify console API, for work the Service API cannot do.

    Authenticates with a console access token::

        console = DifyManagement()                    # reads DIFY_CONSOLE_TOKEN
        console = DifyManagement(token="ey…")

    ``login()`` exchanges an email and password for one. The password is sent
    base64-encoded because that is what the server expects — note that this is
    obfuscation, not encryption, and it is never stored on the client.

    Pass ``http_client`` to supply your own ``httpx.Client``, for a proxy, a
    custom TLS setup, or a transport that does not leave the test process.
    """

    def __init__(
        self,
        token: ApiKeyInput = None,
        base_url: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
        csrf_token: str | None = None,
    ):
        resolved_timeout = check_timeout(timeout, http_client)
        try:
            self._token = resolve_api_key(
                token,
                env_var=CONSOLE_TOKEN_ENV,
                label="Dify console token",
                argument="token",
            )
        except ValueError as error:
            raise ValidationError(str(error)) from error

        if not self._token.is_provider:
            _reject_openapi_token(self._token.reveal())

        self.base_url = resolve_host(base_url, DEFAULT_CONSOLE_URL)
        # Dify 1.17 moved console auth to cookies guarded by a CSRF token; a
        # bearer header alone is rejected. Older versions accept the bearer, so
        # both are sent and whichever the server honours wins.
        self._csrf_token = csrf_token or os.environ.get(CONSOLE_CSRF_ENV) or None
        self.timeout = resolved_timeout
        self._client = http_client or httpx.Client(
            base_url=f"{self.base_url}/console/api",
            timeout=httpx.Timeout(resolved_timeout, connect=5.0),
        )

        from .resources.management import Agents, Apps, Models, Skills, Tools

        #: The workspace's apps, and the steps from a definition to a run.
        #: The workspace's apps, its keys and its triggers.
        self.apps = Apps(self)
        #: Agent skills.
        self.skills = Skills(self)
        #: Agents, which Dify keeps off the app list.
        self.agents = Agents(self)
        #: What the workspace can call, and whether credentials are in place.
        self.models = Models(self)
        #: Installed tool providers and plugins.
        self.tools = Tools(self)

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def login(
        cls,
        email: str,
        password: str,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> DifyManagement:
        """Exchange an account email and password for a console client.

        Prefer passing a token you obtained once: this hands the SDK the
        credential to your whole account, and Dify offers nothing narrower.
        The password is used for this one request and never stored.
        """
        host = resolve_host(base_url, DEFAULT_CONSOLE_URL)
        body = {
            "email": email,
            # The server base64-decodes this field. It is obfuscation for
            # transport, not encryption; HTTPS is what protects it.
            "password": base64.b64encode(password.encode()).decode(),
            "language": "en-US",
        }
        # Named here as everywhere else: a failed login in Dify's log should
        # say what tried, and this request is made before any client exists.
        agent = {"User-Agent": user_agent(None)}
        if http_client is not None:
            response = http_client.post("/login", json=body, headers=agent)
        else:
            with httpx.Client(
                base_url=f"{host}/console/api",
                timeout=httpx.Timeout(check_timeout(timeout, None), connect=5.0),
            ) as client:
                response = client.post("/login", json=body, headers=agent)
        payload = _payload(response)
        if payload.get("result") != "success":
            data = payload.get("data")
            detail = data if isinstance(data, str) else payload.get("message", "")
            msg = f"Console login failed: {detail or 'no detail given'}"
            raise AuthenticationError(msg)

        # Dify 1.17 returns {"result": "success", "data": null} and puts the
        # tokens in Set-Cookie. Earlier versions put them in the body, so read
        # the cookies first and fall back to the body.
        token = response.cookies.get("access_token")
        csrf = response.cookies.get("csrf_token")
        data = payload.get("data")
        if not token and isinstance(data, dict):
            token = data.get("access_token")
        if not token:
            msg = (
                "Console login succeeded but returned no access token, in "
                "neither the response body nor an access_token cookie."
            )
            raise AuthenticationError(msg)
        return cls(
            token=token,
            base_url=host,
            timeout=timeout,
            http_client=http_client,
            csrf_token=csrf,
        )

    def __enter__(self) -> DifyManagement:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def token(self) -> str:
        """The console token itself."""
        return self._token.reveal()

    def __repr__(self) -> str:
        return f"DifyManagement(base_url={self.base_url!r}, token={self._token!r})"

    # -- apps --------------------------------------------------------------

    def _import_app(
        self,
        yaml_content: str,
        *,
        app_id: str | None = None,
        name: str | None = None,
    ) -> ImportResult:
        """Create an app from a DSL document, or overwrite an existing one.

        Pass ``app_id`` to overwrite. Overwriting is what keeps code the source
        of truth: the app in Dify is replaced by what the document says, rather
        than drifting away from it through edits in the UI.
        """
        body: dict[str, Any] = {"mode": "yaml-content", "yaml_content": yaml_content}
        if app_id:
            body["app_id"] = app_id
        if name:
            body["name"] = name
        return _import_result(
            _payload(
                self._client.post("/apps/imports", json=body, headers=self._headers())
            )
        )

    def _confirm_import(self, import_id: str) -> ImportResult:
        """Confirm an import Dify held back over a DSL version difference."""
        return _import_result(
            _payload(
                self._client.post(
                    f"/apps/imports/{import_id}/confirm",
                    headers=self._headers(),
                )
            )
        )

    def _deploy(
        self,
        deployable: Deployable,
        *,
        app_id: str | None = None,
        name: str | None = None,
    ) -> ImportResult:
        """Push a code-defined Workflow or Agent to Dify.

        ``app_id`` overwrites that app. Dify refuses this for Agents — its
        importer only creates new Agent apps — so passing one is rejected here
        rather than failing halfway through.

        Secrets are blanked in the document, as they are in every export: set
        their values in Dify once, and they survive later deploys.
        """
        if app_id and _is_agent(deployable):
            msg = (
                "Dify's importer only creates new Agent apps; it cannot "
                "overwrite one. Deploy without app_id and keep the new id, or "
                "edit the agent in the console."
            )
            raise ValidationError(msg)
        result = self._import_app(
            deployable.to_yaml(),
            app_id=app_id,
            name=name or deployable.name,
        )
        return result.raise_for_status()

    def _export_app(self, app_id: str, *, include_secret: bool = False) -> str:
        """Export an app's DSL, the way the console's Export button does.

        This is the practical starting point for anything whose schema this SDK
        cannot build from scratch — an Agent, most of all. Export it, commit
        the YAML, and edit from there.

        ``include_secret`` asks Dify to leave credentials in. Only do that for
        output going somewhere as guarded as the secrets themselves.
        """
        payload = _payload(
            self._client.get(
                f"/apps/{app_id}/export",
                params={"include_secret": str(include_secret).lower()},
                headers=self._headers(),
            )
        )
        data = payload.get("data")
        if not isinstance(data, str) or not data:
            msg = f"Dify returned no DSL for app {app_id}."
            raise ValidationError(msg)
        return data

    def _publish_workflow(
        self,
        app_id: str,
        *,
        name: str = "",
        comment: str = "",
    ) -> None:
        """Publish an app's draft workflow.

        Importing a DSL only writes the **draft**; the Service API runs the
        published version. Without this step a freshly imported app answers
        every run with "workflow not published".
        """
        _payload(
            self._client.post(
                f"/apps/{app_id}/workflows/publish",
                json={"marked_name": name, "marked_comment": comment},
                headers=self._headers(),
            )
        )

    def _delete_app(self, app_id: str) -> None:
        """Delete an app. There is no undo."""
        response = self._client.delete(f"/apps/{app_id}", headers=self._headers())
        if response.status_code not in (200, 204):
            _payload(response)

    def mint_openapi_token(
        self,
        *,
        client_id: str = "dify-python-sdk",
        device_label: str = "dify-python-sdk",
    ) -> str:
        """Mint an ``/openapi/v1`` OAuth bearer using this console session.

        Dify's device flow normally sends a person to a browser to approve the
        request. This approves it with the session already in hand, which is
        the whole flow without the browser::

            token = DifyManagement.login(email, password).mint_openapi_token()
            client = OpenApiClient(token=token)

        ``client_id`` must appear in the deployment's ``OPENAPI_KNOWN_CLIENT_IDS``,
        which ships holding only ``difyctl``; an unknown one is refused with an
        explanation of what the operator has to add.
        """
        base = f"{self.base_url}{_OPENAPI_DEVICE_PATH}"
        started = self._client.post(
            f"{base}/code",
            json={"client_id": client_id, "device_label": device_label},
        )
        # A refused client answers 400 with {"error": ...}; read it rather than
        # letting the generic handler turn it into a bare APIError.
        try:
            payload = started.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if "user_code" not in payload:
            error = payload.get("error", "")
            if error == "unsupported_client":
                msg = (
                    f"Dify does not know the client id {client_id!r}. Its "
                    "OPENAPI_KNOWN_CLIENT_IDS setting lists which clients may "
                    "mint tokens, and ships holding only 'difyctl'. Ask the "
                    "operator to add this one, or pass client_id='difyctl'."
                )
            else:
                msg = (
                    f"Could not start the device flow: {error or 'no detail given'}. "
                    f"{_OPENAPI_DEVICE_PATH} needs OPENAPI_ENABLED=true."
                )
            raise ValidationError(msg)

        approved = self._client.post(
            f"{base}/approve",
            json={"user_code": payload["user_code"]},
            headers=self._headers(),
        )
        _payload(approved)

        issued = _payload(
            self._client.post(
                f"{base}/token",
                json={"client_id": client_id, "device_code": payload["device_code"]},
            )
        )
        token = issued.get("token") or issued.get("access_token")
        if not token:
            msg = f"The device flow was approved but issued no token: {issued}"
            raise ValidationError(msg)
        return str(token)

    def open_api(self, *, client_id: str = "dify-python-sdk", **kwargs: Any) -> Any:
        """An :class:`~dify_client.openapi.OpenApiClient` built from this session."""
        from .openapi import OpenApiClient

        return OpenApiClient(
            token=self.mint_openapi_token(client_id=client_id),
            base_url=self.base_url,
            **kwargs,
        )

    # -- agents ------------------------------------------------------------

    def _agents(self, *, page: int = 1, limit: int = 20) -> list[AgentSummary]:
        """List the workspace's Agents.

        Agents live on their own roster rather than in the generic app list, so
        neither ``/console/api/apps`` nor ``OpenApiClient.apps()`` shows them.
        """
        payload = _payload(
            self._client.get(
                "/agent",
                params={"page": page, "limit": limit},
                headers=self._headers(),
            )
        )
        return [
            AgentSummary(
                id=str(item.get("id", "")),
                app_id=str(item.get("app_id", "")),
                name=str(item.get("name", "")),
                role=str(item.get("role") or ""),
                description=str(item.get("description") or ""),
                published=bool(item.get("active_config_is_published")),
            )
            for item in payload.get("data", [])
        ]

    def _publish_agent(self, app_id: str) -> str:
        """Publish an Agent app's draft, and report the snapshot it published.

        An Agent app has no workflow draft, so `/apps/<id>/workflows/publish`
        refuses it — but it is not live on import either: Dify holds the Soul
        as a draft until this, and answers a key request with "Publish the
        Agent before enabling Web App or API access". The roster is where the
        publish lives, keyed by the *agent* id rather than the app id.
        """
        agent = next((a for a in self._agents(limit=100) if a.app_id == app_id), None)
        if agent is None:
            msg = (
                f"App {app_id} has no Agent on the roster, so there is no Agent "
                "draft to publish. Agent apps are created by importing an Agent "
                "DSL; a workflow app publishes through apps.publish() instead."
            )
            raise ValidationError(msg)
        payload = _payload(
            self._client.post(
                f"/agent/{agent.id}/publish",
                json={"version_note": "dify-python-sdk"},
                headers=self._headers(),
            )
        )
        return str(payload.get("active_config_snapshot_id") or "")

    def _agent(self, name: str) -> AgentSummary:
        """Find one Agent by name."""
        found = [a for a in self._agents(limit=100) if a.name == name]
        if not found:
            msg = f"This workspace has no Agent named {name!r}."
            raise ValidationError(msg)
        return found[0]

    # -- skills ------------------------------------------------------------

    def _skills(self, *, page: int = 1, limit: int = 50) -> list[WorkspaceSkill]:
        """Skills installed in this workspace."""
        payload = _payload(
            self._client.get(
                "/workspaces/current/skills",
                params={"page": page, "limit": limit},
                headers=self._headers(),
            )
        )
        return [_workspace_skill(item) for item in payload.get("data", [])]

    def _import_skill(self, skill: Skill, *, publish: bool = True) -> WorkspaceSkill:
        """Upload a skill package to the workspace.

        Dify stores an imported skill as a draft. An Agent binds to a
        *published* version, so this publishes by default — the same trap a
        workflow has, where importing alone leaves nothing runnable.
        """
        skill.validate()
        headers = {k: v for k, v in self._headers().items() if k != "Content-Type"}
        response = self._client.post(
            "/workspaces/current/skills/import",
            files={"file": (f"{skill.name}.zip", skill.archive(), "application/zip")},
            headers=headers,
        )
        created = _workspace_skill(_payload(response))
        if publish:
            self._publish_skill(created.id)
            return self._skill(created.name)
        return created

    def _publish_skill(self, skill_id: str, *, note: str = "") -> int:
        """Publish a skill's draft and return the new version number."""
        payload = _payload(
            self._client.post(
                f"/workspaces/current/skills/{skill_id}/publish",
                json={"publish_note": note} if note else {},
                headers=self._headers(),
            )
        )
        return int(payload.get("version_number") or 0)

    def _skill(self, name: str) -> WorkspaceSkill:
        """Find one workspace skill by name."""
        found = [s for s in self._skills(limit=100) if s.name == name]
        if not found:
            msg = f"This workspace has no skill named {name!r}. Import it first."
            raise ValidationError(msg)
        return found[0]

    def _delete_skill(
        self, skill: WorkspaceSkill | str, *, name: str | None = None
    ) -> None:
        """Remove a skill from the workspace.

        Dify asks for the skill's name back as confirmation, the way it does in
        the UI — an Agent bound to a deleted skill loses it. Passing a
        :class:`~dify_client.skills.WorkspaceSkill` supplies the name; passing
        a bare id needs ``name=``.
        """
        skill_id = skill if isinstance(skill, str) else skill.id
        confirmation = name if name is not None else getattr(skill, "name", None)
        if not confirmation:
            msg = (
                "Deleting a skill needs its name as confirmation. Pass the "
                "WorkspaceSkill, or name= alongside the id."
            )
            raise ValidationError(msg)

        response = self._client.request(
            "DELETE",
            f"/workspaces/current/skills/{skill_id}",
            json={"confirmation_name": confirmation},
            headers=self._headers(),
        )
        if response.status_code not in (200, 204):
            _payload(response)

    # -- tools and plugins -------------------------------------------------

    def _tools(self) -> ToolCatalog:
        """What tools this workspace can offer a workflow.

        The identifiers a tool node needs come from the workspace, so this is
        the online step that makes ``wf.tool(...)`` possible offline::

            catalog = console.tools()
            wf.tool(catalog["time"]["current_time"], config={"timezone": "Asia/Tokyo"})
        """
        listing = _payload(
            self._client.get(
                "/workspaces/current/tool-providers", headers=self._headers()
            )
        )
        providers = listing.get("data") if isinstance(listing, dict) else None
        if providers is None:
            # The endpoint answers with a bare list.
            raw = self._client.get(
                "/workspaces/current/tool-providers", headers=self._headers()
            ).json()
            providers = raw if isinstance(raw, list) else []

        parsed = []
        for provider in providers:
            name = provider.get("name") or provider.get("id")
            kind = provider.get("type", "builtin")
            tools = self._tools_of(name, kind)
            parsed.append(parse_provider(provider, tools))
        return ToolCatalog(parsed)

    def _tools_of(self, provider: str, kind: str) -> list[dict[str, Any]]:
        """A provider's tools. Only builtin-shaped providers expose them here."""
        if kind not in {"builtin", "plugin", "model"}:
            return []
        response = self._client.get(
            f"/workspaces/current/tool-provider/builtin/{provider}/tools",
            headers=self._headers(),
        )
        if response.status_code != 200:
            return []
        try:
            body = response.json()
        except ValueError:
            return []
        return body if isinstance(body, list) else body.get("data", [])

    def _plugins(self) -> list[dict[str, Any]]:
        """Plugins installed in this workspace, with their exact identifiers."""
        payload = _payload(
            self._client.get("/workspaces/current/plugin/list", headers=self._headers())
        )
        return list(payload.get("plugins", []))

    def _plugin_identifier(self, plugin_id: str) -> str:
        """The installed identifier for ``plugin_id``, for ``wf.depends_on()``.

        Declaring a version other than the one installed makes Dify try to
        fetch it on import, so read the real one rather than copying a hash
        from a marketplace page::

            wf.depends_on(console.plugin_identifier("langgenius/openai"))
        """
        installed = self._plugins()
        for plugin in installed:
            if plugin.get("plugin_id") == plugin_id:
                identifier = plugin.get("plugin_unique_identifier")
                if identifier:
                    return str(identifier)
        known = ", ".join(sorted(str(p.get("plugin_id")) for p in installed)) or "none"
        msg = (
            f"{plugin_id!r} is not installed in this workspace. Installed: {known}. "
            "Install it in Dify first, or declare the identifier by hand."
        )
        raise ValidationError(msg)

    # -- app API keys ------------------------------------------------------

    def _list_api_keys(self, app_id: str) -> list[ApiKey]:
        """List an app's Service-API keys. Tokens come back masked."""
        payload = _payload(
            self._client.get(f"/apps/{app_id}/api-keys", headers=self._headers())
        )
        return [
            ApiKey(
                id=item.get("id", ""),
                token=item.get("token", ""),
                type=item.get("type", "app"),
                created_at=item.get("created_at"),
            )
            for item in payload.get("data", [])
        ]

    def _create_api_key(self, app_id: str) -> ApiKey:
        """Create a Service-API key for an app and return it in full.

        This is the only time Dify reveals the whole token, and an app may only
        have so many, so store what comes back rather than calling this per run.
        """
        payload = _payload(
            self._client.post(f"/apps/{app_id}/api-keys", headers=self._headers())
        )
        return ApiKey(
            id=payload.get("id", ""),
            token=payload.get("token", ""),
            type=payload.get("type", "app"),
            created_at=payload.get("created_at"),
        )

    def _delete_api_key(self, app_id: str, key_id: str) -> None:
        """Revoke one of an app's Service-API keys."""
        response = self._client.delete(
            f"/apps/{app_id}/api-keys/{key_id}", headers=self._headers()
        )
        if response.status_code not in (200, 204):
            _payload(response)

    # -- finding apps ------------------------------------------------------

    def _apps(
        self,
        *,
        mode: str | None = None,
        name: str | None = None,
        page: int = 1,
        limit: int = 30,
    ) -> Page[App]:
        """List the workspace's apps.

        Args:
            mode: Only apps of this mode — ``workflow``, ``advanced-chat``,
                ``chat``, ``completion`` or ``agent-chat``.
            name: Match apps whose name contains this.
            page: Page number, from 1.
            limit: How many per page.

        Agents are not apps and do not appear here; use :meth:`agents`.
        """

        def fetch(number: int) -> dict[str, Any]:
            params: dict[str, Any] = {"page": number, "limit": limit}
            if mode:
                params["mode"] = mode
            if name:
                params["name"] = name
            return _payload(
                self._client.get("/apps", params=params, headers=self._headers())
            )

        return by_page(fetch(page), _app_summary, fetch, page)

    def _app(self, name_or_id: str) -> App:
        """Find one app by id, or by exact name.

        Walks every page. It used to look at the first hundred and report a
        real app as missing, which is the kind of wrong answer that costs an
        afternoon.

        Raises :class:`ValidationError` when nothing matches, or when a name
        matches more than one app — in which case pass the id instead.
        """
        found = [
            app
            for app in self._apps(limit=100).all()
            if app.id == name_or_id or app.name == name_or_id
        ]
        if not found:
            msg = (
                f"No app called {name_or_id!r} in this workspace. "
                "management.apps.list() shows what is there."
            )
            raise ValidationError(msg)
        if len(found) > 1:
            ids = ", ".join(a.id for a in found)
            msg = f"{len(found)} apps are called {name_or_id!r}. Pass one of: {ids}."
            raise ValidationError(msg)
        return found[0]

    # -- triggers ----------------------------------------------------------

    def _triggers(self, app_id: str) -> list[Trigger]:
        """The triggers a published workflow has, enabled or not.

        A trigger node in a draft is only a drawing. Dify materializes the
        trigger — and, for a webhook, its URL — when the workflow is published,
        so publish before reading this back.
        """
        payload = _payload(
            self._client.get(f"/apps/{app_id}/triggers", headers=self._headers())
        )
        return [_trigger(item) for item in payload.get("data", [])]

    def _webhook_trigger(self, app_id: str, node_id: str) -> WebhookTrigger:
        """The URL Dify minted for one webhook trigger node.

        ``node_id`` is the id of the ``wf.webhook(...)`` node, which the builder
        names ``trigger_webhook`` unless told otherwise.
        """
        payload = _payload(
            self._client.get(
                f"/apps/{app_id}/workflows/triggers/webhook",
                params={"node_id": node_id},
                headers=self._headers(),
            )
        )
        return WebhookTrigger(
            id=str(payload.get("id", "")),
            webhook_id=str(payload.get("webhook_id", "")),
            url=str(payload.get("webhook_url", "")),
            debug_url=str(payload.get("webhook_debug_url", "")),
            node_id=str(payload.get("node_id", "")),
        )

    def _enable_trigger(
        self, app_id: str, trigger_id: str, *, enabled: bool = True
    ) -> Trigger:
        """Turn one trigger on or off, leaving the workflow as it is.

        A trigger arrives enabled. This is the switch for pausing a schedule or
        a webhook without unpublishing the app.
        """
        payload = _payload(
            self._client.post(
                f"/apps/{app_id}/trigger-enable",
                json={"trigger_id": trigger_id, "enable_trigger": enabled},
                headers=self._headers(),
            )
        )
        return _trigger(payload)

    # -- models ------------------------------------------------------------

    def _model_providers(self) -> list[ModelProvider]:
        """Model providers configured in this workspace.

        Each entry reports whether credentials are in place, which is what
        decides whether a workflow's LLM node can actually run.
        """
        payload = _payload(
            self._client.get(
                "/workspaces/current/model-providers", headers=self._headers()
            )
        )
        return providers_from(payload.get("data", []))

    def _models(self, model_type: str = "llm") -> list[ModelProvider]:
        """Models this workspace can use, by type.

        ``model_type`` is Dify's own vocabulary: ``llm``, ``text-embedding``,
        ``rerank``, ``speech2text``, ``tts``, ``moderation``.
        """
        payload = _payload(
            self._client.get(
                f"/workspaces/current/models/model-types/{model_type}",
                headers=self._headers(),
            )
        )
        return providers_from(payload.get("data", []))

    def _model_names(self, model_type: str = "llm") -> list[str]:
        """Model references ready for ``wf.llm(model=...)`` and ``Agent.create``."""
        return [
            f"{model.provider}:{model.name}"
            for provider in self._models(model_type)
            for model in provider
            if model.usable
        ]

    # -- internals ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        token = self._token.reveal()
        cookies = [f"access_token={token}"]
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": user_agent(self._client.headers.get("User-Agent")),
        }
        if self._csrf_token:
            cookies.append(f"csrf_token={self._csrf_token}")
            headers[CSRF_HEADER] = self._csrf_token
        headers["Cookie"] = "; ".join(cookies)
        return headers


def _reject_openapi_token(token: str) -> None:
    """Refuse an ``/openapi/v1`` bearer handed to the console client.

    The two are easy to mix up — ``difyctl`` stores one and this client wants
    the other — and the console API answers a ``dfoa_`` token with a flat 401,
    which says nothing about why.
    """
    if token.startswith(OPENAPI_TOKEN_PREFIXES):
        msg = (
            "This looks like an /openapi/v1 OAuth bearer (the kind difyctl "
            "mints and keeps in DIFY_CONSOLE_TOKEN). DifyManagement speaks to "
            f"/console/api, which needs a console session: set "
            f"{CONSOLE_TOKEN_ENV}, or call DifyManagement.login(email, password)."
        )
        raise ValidationError(msg)


def _trigger(payload: Mapping[str, Any]) -> Trigger:
    return Trigger(
        id=str(payload.get("id", "")),
        type=str(payload.get("trigger_type", "")),
        title=str(payload.get("title", "")),
        node_id=str(payload.get("node_id", "")),
        status=str(payload.get("status", "")),
        provider_name=str(payload.get("provider_name") or ""),
    )


def _workspace_skill(payload: Mapping[str, Any]) -> WorkspaceSkill:
    version = payload.get("latest_published_version_number")
    return WorkspaceSkill(
        id=str(payload.get("id", "")),
        name=str(payload.get("name", "")),
        description=str(payload.get("description") or ""),
        display_name=str(payload.get("display_name") or ""),
        published_version=int(version) if version is not None else None,
        reference_count=int(payload.get("reference_count") or 0),
    )


def _is_agent(deployable: Any) -> bool:
    """Whether this is an Agent rather than a Workflow."""
    return getattr(deployable, "mode", None) == "agent"


def _payload(response: httpx.Response) -> dict[str, Any]:
    """Return a console response body, turning failures into SDK errors."""
    if response.status_code == 401:
        detail = ""
        try:
            detail = str(response.json().get("message") or "")
        except ValueError:
            pass
        if "csrf" in detail.lower():
            msg = (
                "Dify rejected this write for a missing CSRF token. Since 1.17 "
                "the console API pairs the access token with a second one, and "
                "an access token alone can read but not write. Either use "
                "DifyManagement.login(email, password), which collects both, or "
                f"set {CONSOLE_CSRF_ENV} alongside {CONSOLE_TOKEN_ENV} — the "
                "browser holds it as the csrf_token cookie."
            )
        else:
            msg = (
                "The console token was rejected. Console tokens are short-lived; "
                f"get a fresh one and set {CONSOLE_TOKEN_ENV}, or use "
                "DifyManagement.login(email, password)."
            )
        raise AuthenticationError(msg, status_code=401, headers=response.headers)

    try:
        body = response.json()
    except ValueError:
        body = {}

    # An import that fails still answers 400 with a usable body; let the caller
    # read the status rather than raising over it.
    if response.status_code >= 400 and not isinstance(body.get("status"), str):
        message = (
            body.get("message") or body.get("error") or f"HTTP {response.status_code}"
        )
        raise APIError(
            str(message),
            status_code=response.status_code,
            response=body,
            headers=response.headers,
        )
    return body if isinstance(body, dict) else {}


def _import_result(payload: dict[str, Any]) -> ImportResult:
    return ImportResult(
        id=payload.get("id", ""),
        status=payload.get("status", ""),
        app_id=payload.get("app_id"),
        app_mode=payload.get("app_mode"),
        imported_dsl_version=payload.get("imported_dsl_version", ""),
        current_dsl_version=payload.get("current_dsl_version", ""),
        error=payload.get("error", ""),
        warnings=list(payload.get("warnings", [])),
    )
