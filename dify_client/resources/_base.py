"""What every resource shares: the transport it speaks through."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """The part of a Service-API client a resource is allowed to use.

    Resources took ``Any`` and reached for whatever they liked, so wiring one
    to the wrong client — a knowledge resource onto a console client, say — was
    only found at runtime, by a 404. This is the contract instead: a transport
    sends requests and knows its own base URL, and nothing else about it is a
    resource's business.

    Deliberately loose about what ``_send_request`` returns: the sync transport
    returns a response and the async one returns an awaitable, and the async
    resources are the same code with ``await`` in front. Naming one return type
    here would make every async resource a type error for describing exactly
    what it does.
    """

    base_url: str
    #: The end-user identifier to send when a call does not name one.
    default_user: str

    # Spelled to match the concrete transports exactly: a Protocol parameter
    # is contravariant, so widening `dict` to `Mapping` here would make the
    # real clients fail to satisfy it.
    def _send_request(
        self,
        method: str,
        endpoint: str,
        json: dict[str, Any] | None = ...,
        params: dict[str, Any] | None = ...,
        stream: bool = ...,
        **kwargs: Any,
    ) -> Any: ...

    def _send_request_with_files(
        self, method: str, endpoint: str, data: dict, files: dict
    ) -> Any: ...


@runtime_checkable
class Console(Protocol):
    """The part of a console client a management resource may use.

    A different contract from :class:`Transport`, and saying so is the point:
    the console speaks in whole operations — import this, publish that — rather
    than in requests, and a Service-API key cannot perform any of them.
    """

    base_url: str

    def _headers(self) -> dict[str, str]: ...


class Resource:
    """Verbs for one kind of thing, borrowing a client's connection.

    A resource owns no credential and no connection of its own. It is a view
    onto the client that made it, so closing the client closes everything.
    """

    def __init__(self, client: Transport | Console) -> None:
        self._client: Any = client

    @property
    def _user_default(self) -> str:
        return getattr(self._client, "default_user", "") or ""

    def _who(self, user: str | None) -> str:
        """The end-user identifier for this call.

        Dify wants one on nearly every request. Setting it once on the client
        is the usual case; passing it per call is for a server handling many.
        """
        resolved = user or self._user_default
        if not resolved:
            from ..exceptions import ValidationError

            msg = (
                "Dify needs an end-user identifier for this call. Pass user=…, "
                "or set it once with DifyApp(..., user='…')."
            )
            raise ValidationError(msg)
        return resolved

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._client!r})"
