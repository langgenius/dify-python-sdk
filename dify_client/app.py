"""One app on Dify, and everything you can do to it.

The client holds the connection and the credential. What you can *do* hangs off
it by what it acts on::

    app = DifyApp(api_key="app-…")

    message = app.chat.messages.create("Hello", user="alice")
    run = app.workflows.runs.create({"text": "…"}, user="alice")

A Service-API key belongs to one app, so this client is that app. The
workspace-level surfaces are separate because their credentials are:
:class:`~dify_client.DifyKnowledge` for datasets, and
:class:`~dify_client.DifyManagement` for managing apps.

An app has one mode, and Dify serves each mode on its own route — so
``app.chat`` on a workflow app, or ``app.workflows`` on a chatflow, answers
"check if your app mode matches the right API route". :meth:`DifyApp.info`
reports which this is; ``DifyApp.open()`` asks before handing the client back.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from ._transport import Transport
from .paging import by_page, by_page_async
from .resources import (
    Annotations,
    AsyncAnnotations,
    AsyncAudio,
    AsyncCompletions,
    AsyncConversations,
    AsyncFiles,
    AsyncForms,
    AsyncMessages,
    AsyncWorkflowRuns,
    Audio,
    Completions,
    Conversations,
    Files,
    Forms,
    Messages,
    WorkflowRuns,
)
from .results import AsyncPage, Page
from .secrets import ApiKeyInput

__all__ = [
    "AppInfo",
    "AppParameters",
    "AsyncChat",
    "AsyncDifyApp",
    "AsyncWorkflows",
    "Chat",
    "DifyApp",
    "InputField",
    "ServerInfo",
    "SiteSettings",
    "Workflows",
]


@dataclass(frozen=True)
class ServerInfo:
    """What the Service API says about itself, before any credential.

    The only place Dify reliably reports its running version:
    ``/openapi/v1/_version`` is off unless ``OPENAPI_ENABLED`` is set, and
    ``/console/api/version`` answers with the *latest released* version rather
    than the one you are talking to.
    """

    server_version: str = ""
    api_version: str = ""
    welcome: str = ""

    def __str__(self) -> str:
        return f"Dify {self.server_version} ({self.api_version})".strip()


@dataclass(frozen=True)
class AppInfo:
    """What Dify says this app is."""

    name: str = ""
    mode: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    author_name: str = ""

    @property
    def is_chat(self) -> bool:
        return self.mode in {"chat", "advanced-chat", "agent-chat"}

    @property
    def is_workflow(self) -> bool:
        return self.mode == "workflow"


@dataclass(frozen=True)
class InputField:
    """One field an app declares, as its start node defines it.

    ``name`` is the key to put in ``inputs``. Dify spells it ``variable`` and
    puts a separate ``label`` beside it for display; using the label as the key
    is the usual first mistake, because for a field created in the UI the two
    are often the same string and it works until someone renames one.
    """

    name: str
    label: str = ""
    #: ``text-input``, ``paragraph``, ``select``, ``number``, ``file``,
    #: ``file-list``, ``checkbox``, ``json_object`` or ``external_data_tool``.
    type: str = ""
    required: bool = False
    options: tuple[str, ...] = ()
    default: Any = None
    max_length: int | None = None
    description: str = ""
    hidden: bool = False
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class AppParameters:
    """What an app declares it takes, and what it has turned on.

    Iterating gives the input fields, and ``parameters["topic"]`` picks one out
    by the name you would pass in ``inputs`` — the listing arrives as a list of
    single-key objects (``{"text-input": {...}}``), which is a shape nobody
    should have to unwrap to find out whether a field is required.
    """

    inputs: tuple[InputField, ...] = ()
    opening_statement: str = ""
    suggested_questions: tuple[str, ...] = ()
    #: The feature toggles, flattened: Dify sends each as ``{"enabled": bool}``.
    features: Mapping[str, bool] = field(default_factory=dict)
    #: What the app accepts as uploads. Shape depends on the features enabled.
    file_upload: dict[str, Any] = field(default_factory=dict)
    #: The deployment's own limits — file sizes in MB, uploads per workflow.
    system_parameters: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def __iter__(self) -> Iterator[InputField]:
        return iter(self.inputs)

    def __len__(self) -> int:
        return len(self.inputs)

    def __getitem__(self, name: str) -> InputField:
        found = self.get(name)
        if found is None:
            known = ", ".join(field_.name for field_ in self.inputs) or "none"
            msg = f"This app declares no input called {name!r}. It takes: {known}."
            raise KeyError(msg)
        return found

    def get(self, name: str) -> InputField | None:
        """The field with this name, or None."""
        for declared in self.inputs:
            if declared.name == name:
                return declared
        return None

    @property
    def required(self) -> tuple[InputField, ...]:
        """The fields a run will be rejected without."""
        return tuple(one for one in self.inputs if one.required)


@dataclass(frozen=True)
class SiteSettings:
    """The WebApp's own settings: what a visitor sees before typing anything."""

    title: str = ""
    description: str = ""
    icon: str = ""
    icon_type: str = ""
    icon_background: str = ""
    icon_url: str = ""
    default_language: str = ""
    chat_color_theme: str = ""
    chat_color_theme_inverted: bool = False
    input_placeholder: str = ""
    copyright: str = ""
    privacy_policy: str = ""
    custom_disclaimer: str = ""
    show_workflow_steps: bool = False
    use_icon_as_answer_icon: bool = False
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def __str__(self) -> str:
        return self.title


def _input_field(entry: Mapping[str, Any]) -> InputField | None:
    """Unwrap one ``{"text-input": {...}}`` entry.

    Dify keys each field by its kind rather than putting the kind inside, and
    an entry it has no name for is skipped rather than turned into a field
    called "".
    """
    for kind, config in entry.items():
        if not isinstance(config, Mapping):
            continue
        name = str(config.get("variable") or "")
        if not name:
            continue
        options = config.get("options") or []
        length = config.get("max_length")
        return InputField(
            name=name,
            label=str(config.get("label") or ""),
            type=str(config.get("type") or kind),
            required=bool(config.get("required", False)),
            options=tuple(str(option) for option in options),
            default=config.get("default"),
            max_length=int(length) if isinstance(length, int) else None,
            description=str(config.get("description") or ""),
            hidden=bool(config.get("hide", False)),
            payload=dict(config),
        )
    return None


def _app_parameters(payload: Mapping[str, Any]) -> AppParameters:
    """Shape ``/parameters``. Shared so sync and async cannot drift."""
    declared = payload.get("user_input_form")
    fields = [
        shaped
        for entry in (declared if isinstance(declared, list) else [])
        if isinstance(entry, Mapping) and (shaped := _input_field(entry)) is not None
    ]
    features = {
        key: bool(value.get("enabled"))
        for key, value in payload.items()
        if isinstance(value, Mapping) and "enabled" in value
    }
    questions = payload.get("suggested_questions") or []
    return AppParameters(
        inputs=tuple(fields),
        opening_statement=str(payload.get("opening_statement") or ""),
        suggested_questions=tuple(str(one) for one in questions),
        features=features,
        file_upload=dict(payload.get("file_upload") or {}),
        system_parameters=dict(payload.get("system_parameters") or {}),
        payload=dict(payload),
    )


def _site(payload: Mapping[str, Any]) -> SiteSettings:
    """Shape ``/site``. Shared so sync and async cannot drift."""

    def text(key: str) -> str:
        return str(payload.get(key) or "")

    return SiteSettings(
        title=text("title"),
        description=text("description"),
        icon=text("icon"),
        icon_type=text("icon_type"),
        icon_background=text("icon_background"),
        icon_url=text("icon_url"),
        default_language=text("default_language"),
        chat_color_theme=text("chat_color_theme"),
        chat_color_theme_inverted=bool(payload.get("chat_color_theme_inverted", False)),
        input_placeholder=text("input_placeholder"),
        copyright=text("copyright"),
        privacy_policy=text("privacy_policy"),
        custom_disclaimer=text("custom_disclaimer"),
        show_workflow_steps=bool(payload.get("show_workflow_steps", False)),
        use_icon_as_answer_icon=bool(payload.get("use_icon_as_answer_icon", False)),
        payload=dict(payload),
    )


def _server_info(payload: Mapping[str, Any]) -> ServerInfo:
    """Shape the Service API's index. Shared so sync and async cannot drift."""
    return ServerInfo(
        server_version=str(payload.get("server_version") or ""),
        api_version=str(payload.get("api_version") or ""),
        welcome=str(payload.get("welcome") or ""),
    )


def _app_info(payload: Mapping[str, Any]) -> AppInfo:
    """Shape ``/info``. Shared so sync and async cannot drift."""
    tags = payload.get("tags") or []
    return AppInfo(
        name=str(payload.get("name") or ""),
        mode=str(payload.get("mode") or ""),
        description=str(payload.get("description") or ""),
        tags=tuple(str(tag) for tag in tags),
        author_name=str(payload.get("author_name") or ""),
    )


class Chat:
    """The conversational half of an app: messages and the threads they form."""

    def __init__(self, client: Any) -> None:
        self.messages = Messages(client)
        self.conversations = Conversations(client)


class Workflows:
    """The workflow half of an app: runs, and what they did."""

    def __init__(self, client: Any) -> None:
        self.runs = WorkflowRuns(client)


class AsyncChat:
    def __init__(self, client: Any) -> None:
        self.messages = AsyncMessages(client)
        self.conversations = AsyncConversations(client)


class AsyncWorkflows:
    def __init__(self, client: Any) -> None:
        self.runs = AsyncWorkflowRuns(client)


class DifyApp(Transport):
    """One Dify app, addressed by its Service-API key.

    Args:
        api_key: The app's key, or a callable returning one. Left out, it is
            read from ``DIFY_API_KEY``.
        base_url: The Service API root. Left out, derived from ``DIFY_HOST``,
            then ``DIFY_API_BASE_URL``, then Dify Cloud.
        user: The end-user identifier to send when a call does not name one.
            Dify wants one on nearly every request; setting it here is the
            usual case, passing it per call is for a server handling many.
        kwargs: ``timeout``, ``max_retries``, ``retry_delay``,
            ``enable_logging``, ``http_client``.
    """

    def __init__(
        self,
        api_key: ApiKeyInput = None,
        base_url: str | None = None,
        *,
        user: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(api_key, base_url=base_url, **kwargs)
        self.default_user = user
        self.chat = Chat(self)
        self.workflows = Workflows(self)
        self.completions = Completions(self)
        self.files = Files(self)
        self.annotations = Annotations(self)
        self.audio = Audio(self)
        self.forms = Forms(self)

    @classmethod
    def open(
        cls,
        api_key: ApiKeyInput = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> DifyApp:
        """Open an app and check its mode before returning it.

        Costs one request. Use it when the key comes from configuration and you
        would rather find out here than on the first run.
        """
        app = cls(api_key, base_url=base_url, **kwargs)
        app.info()
        return app

    def server_info(self) -> ServerInfo:
        """What this Dify is, before any credential is involved.

        The Service API's own index. It is the only place the running version
        is reliably reported: ``/openapi/v1/_version`` is off unless
        ``OPENAPI_ENABLED`` is set, and ``/console/api/version`` answers with
        the *latest released* version rather than the one you are talking to.

        Sent through this client so a proxy or a custom TLS setup applies, but
        the key is beside the point — the endpoint takes none.
        """
        return _server_info(self._send_request("GET", "/").json())

    @staticmethod
    def probe(base_url: str | None = None, *, timeout: float = 5.0) -> ServerInfo:
        """Ask a Dify what version it is, with no client and no credential.

        For finding out whether a host is a Dify at all — an unreachable host
        and a wrong key look the same once authenticated calls start.
        """
        from .secrets import resolve_base_url

        resolved = resolve_base_url(base_url, DifyApp.DEFAULT_BASE_URL)
        with httpx.Client(timeout=timeout) as http:
            payload = http.get(f"{resolved.rstrip('/')}/").raise_for_status().json()
        return _server_info(payload)

    def info(self) -> AppInfo:
        """What this app is: its name, and the mode that decides its routes."""
        return _app_info(self._send_request("GET", "/info").json())

    def parameters(self, *, user: str | None = None) -> AppParameters:
        """The app's declared inputs, features and limits.

        ``parameters.inputs`` are the keys a run takes; ``parameters["topic"]``
        picks one out, and ``.required`` is what a run is rejected without.
        """
        who = user or self.default_user or "dify-python-sdk"
        payload = self._send_request("GET", "/parameters", params={"user": who}).json()
        return _app_parameters(payload)

    def meta(self, *, user: str | None = None) -> dict[str, Any]:
        """The app's tool icons and other display metadata."""
        who = user or self.default_user or "dify-python-sdk"
        return self._send_request("GET", "/meta", params={"user": who}).json()

    def site(self) -> SiteSettings:
        """The WebApp settings: title, icon, theme, what visitors may do."""
        return _site(self._send_request("GET", "/site").json())

    def feedbacks(self, *, page: int = 1, limit: int = 20) -> Page[dict[str, Any]]:
        """Every rating left on this app's messages, end users and admins alike."""

        def fetch(number: int) -> dict[str, Any]:
            return self._send_request(
                "GET", "/app/feedbacks", params={"page": number, "limit": limit}
            ).json()

        return by_page(fetch(page), dict, fetch, page)

    def end_user(self, end_user_id: str) -> dict[str, Any]:
        """Look up an end user by the id other responses hand back.

        ``created_by`` on an uploaded file is one of these, and means nothing
        until resolved. Scoped to this app, so an id from elsewhere 404s.
        """
        return self._send_request("GET", f"/end-users/{end_user_id}").json()

    def __repr__(self) -> str:
        from .secrets import mask_secret

        return (
            f"DifyApp(base_url={self.base_url!r}, "
            f"api_key={mask_secret(self.api_key)!r})"
        )


class AsyncDifyApp:
    """The async counterpart of :class:`DifyApp`."""

    def __init__(
        self,
        api_key: ApiKeyInput = None,
        base_url: str | None = None,
        *,
        user: str = "",
        **kwargs: Any,
    ) -> None:
        from ._async_transport import AsyncTransport

        self._inner = AsyncTransport(api_key, base_url=base_url, **kwargs)
        self._inner.default_user = user
        self.default_user = user
        self.chat = AsyncChat(self._inner)
        self.workflows = AsyncWorkflows(self._inner)
        self.completions = AsyncCompletions(self._inner)
        self.files = AsyncFiles(self._inner)
        self.annotations = AsyncAnnotations(self._inner)
        self.audio = AsyncAudio(self._inner)
        self.forms = AsyncForms(self._inner)

    @property
    def base_url(self) -> str:
        return self._inner.base_url

    def with_timeout(self, seconds: float | httpx.Timeout | None) -> Any:
        """Give every call inside the block a different timeout.

        Not awaited: entering the block sends nothing.
        """
        return self._inner.with_timeout(seconds)

    async def __aenter__(self) -> AsyncDifyApp:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._inner.aclose()

    @classmethod
    async def open(
        cls,
        api_key: ApiKeyInput = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> AsyncDifyApp:
        """Open an app and check its mode before returning it.

        Costs one request, like :meth:`DifyApp.open`. Awaited rather than
        called, because the check is a request::

            app = await AsyncDifyApp.open()
        """
        app = cls(api_key, base_url=base_url, **kwargs)
        try:
            await app.info()
        except BaseException:
            await app.aclose()
            raise
        return app

    async def server_info(self) -> ServerInfo:
        """What this Dify is, before any credential is involved.

        See :meth:`DifyApp.server_info` for why this endpoint and not another.
        """
        response = await self._inner._send_request("GET", "/")
        return _server_info(response.json())

    @staticmethod
    async def probe(base_url: str | None = None, *, timeout: float = 5.0) -> ServerInfo:
        """Ask a Dify what version it is, with no client and no credential."""
        from .secrets import resolve_base_url

        resolved = resolve_base_url(base_url, Transport.DEFAULT_BASE_URL)
        async with httpx.AsyncClient(timeout=timeout) as http:
            response = await http.get(f"{resolved.rstrip('/')}/")
        return _server_info(response.raise_for_status().json())

    async def info(self) -> AppInfo:
        """What this app is: its name, and the mode that decides its routes."""
        response = await self._inner._send_request("GET", "/info")
        return _app_info(response.json())

    async def parameters(self, *, user: str | None = None) -> AppParameters:
        """The app's declared inputs, features and limits."""
        who = user or self.default_user or "dify-python-sdk"
        response = await self._inner._send_request(
            "GET", "/parameters", params={"user": who}
        )
        return _app_parameters(response.json())

    async def meta(self, *, user: str | None = None) -> dict[str, Any]:
        """The app's tool icons and other display metadata."""
        who = user or self.default_user or "dify-python-sdk"
        response = await self._inner._send_request("GET", "/meta", params={"user": who})
        return dict(response.json())

    async def site(self) -> SiteSettings:
        """The WebApp settings: title, icon, theme, what visitors may do."""
        response = await self._inner._send_request("GET", "/site")
        return _site(response.json())

    async def feedbacks(
        self, *, page: int = 1, limit: int = 20
    ) -> AsyncPage[dict[str, Any]]:
        """Every rating left on this app's messages."""

        async def fetch(number: int) -> dict[str, Any]:
            response = await self._inner._send_request(
                "GET", "/app/feedbacks", params={"page": number, "limit": limit}
            )
            return dict(response.json())

        return by_page_async(await fetch(page), dict, fetch, page)

    async def end_user(self, end_user_id: str) -> dict[str, Any]:
        """Look up an end user by the id other responses hand back."""
        response = await self._inner._send_request("GET", f"/end-users/{end_user_id}")
        return dict(response.json())

    def __repr__(self) -> str:
        from .secrets import mask_secret

        return (
            f"AsyncDifyApp(base_url={self.base_url!r}, "
            f"api_key={mask_secret(self._inner.api_key)!r})"
        )
