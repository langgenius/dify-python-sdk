"""Holding and displaying API keys without leaking them.

A key that is only ever a plain ``str`` attribute shows up in
``vars(client)``, in tracebacks captured with locals, and in whatever error
reporter the application happens to run. ``SecretKey`` keeps the value
reachable for the one caller that needs it — the request that signs itself —
while everything that renders an object sees a masked form.
"""

from __future__ import annotations

import os
from collections.abc import Callable

#: Environment variable read when no API key is passed explicitly.
API_KEY_ENV = "DIFY_API_KEY"

#: Environment variable naming the Dify host, e.g. ``https://cloud.dify.ai``.
#: Shared with the ``difyctl`` CLI, so configuring one configures the other.
HOST_ENV = "DIFY_HOST"

#: Environment variable overriding the Service API base URL on its own, for the
#: unusual case where it does not sit at ``<host>/v1``.
BASE_URL_ENV = "DIFY_API_BASE_URL"

#: An API key, or something that produces one on demand.
ApiKeyInput = str | Callable[[], str] | None


def mask_secret(value: str, *, keep: int = 4) -> str:
    """Render a key the way it is safe to print: ``app-****3f2a``.

    Dify issues keys with a type prefix (``app-``, ``ds-``), which is worth
    keeping so a masked key is still identifiable.
    """
    if not value:
        return "****"
    prefix, sep, rest = value.partition("-")
    head = f"{prefix}{sep}" if sep and len(prefix) <= 8 else ""
    tail = rest if sep and head else value
    # Showing the tail of a short value would reveal most of it, so only do it
    # once enough of the key stays hidden.
    if len(tail) <= keep * 2:
        return f"{head}****"
    return f"{head}****{tail[-keep:]}"


class SecretKey:
    """An API key that does not render itself.

    Construct it from a string, or from a callable that returns one. A
    callable is resolved on every use rather than being cached, which is what
    lets a key come from a vault and be rotated without rebuilding the client.
    """

    __slots__ = ("_static", "_provider")

    def __init__(self, value: str | Callable[[], str]):
        if callable(value):
            self._static: str | None = None
            self._provider: Callable[[], str] | None = value
        else:
            self._static = value
            self._provider = None

    def reveal(self) -> str:
        """Return the key itself. Call this only to authenticate a request."""
        if self._provider is not None:
            resolved = self._provider()
            if not resolved:
                msg = "The api_key provider returned an empty key."
                raise ValueError(msg)
            return resolved
        return self._static or ""

    @property
    def is_provider(self) -> bool:
        """Whether the key is produced on demand rather than held."""
        return self._provider is not None

    def __repr__(self) -> str:
        if self._provider is not None:
            # Resolving here would call the vault just to print an object.
            return "SecretKey(<provider>)"
        return f"SecretKey({mask_secret(self._static or '')!r})"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretKey):
            return self._static == other._static and self._provider is other._provider
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._static, self._provider))

    def __bool__(self) -> bool:
        return self._provider is not None or bool(self._static)


def resolve_api_key(
    api_key: ApiKeyInput = None,
    *,
    env_var: str = API_KEY_ENV,
    label: str = "Dify API key",
    argument: str = "api_key",
) -> SecretKey:
    """Resolve a credential from the argument, then the environment.

    Passing it explicitly — including an empty string — stops the environment
    from being consulted, so a missing value in code surfaces as an error
    rather than silently picking up whatever the shell happens to hold.

    ``label`` and ``argument`` only shape the error messages, so a caller that
    wants a console token is not told to pass an API key.
    """
    if api_key is None:
        from_env = os.environ.get(env_var)
        if not from_env:
            msg = (
                f"No {label}. Pass {argument}=... to the client, "
                f"or set {env_var} in the environment."
            )
            raise ValueError(msg)
        return SecretKey(from_env)

    if callable(api_key):
        return SecretKey(api_key)

    if not api_key:
        msg = f"{argument} is empty. Pass a value, or leave it out to read {env_var}."
        raise ValueError(msg)
    return SecretKey(api_key)


def resolve_host(host: str | None, default: str, *, env_var: str = HOST_ENV) -> str:
    """Resolve the Dify host from the argument, the environment, then ``default``."""
    if host is not None:
        return host.rstrip("/")
    return (os.environ.get(env_var) or default).rstrip("/")


def resolve_base_url(
    base_url: str | None,
    default: str,
    *,
    env_var: str = BASE_URL_ENV,
    host_env: str = HOST_ENV,
    path: str = "/v1",
) -> str:
    """Resolve the Service API base URL.

    In order: the argument, ``DIFY_API_BASE_URL``, then ``<DIFY_HOST>/v1``, then
    ``default``. Deriving from the host means a self-hosted Dify needs one
    variable rather than two, and the same one ``difyctl`` already reads.
    """
    if base_url is not None:
        return base_url.rstrip("/")
    explicit = os.environ.get(env_var)
    if explicit:
        return explicit.rstrip("/")
    host = os.environ.get(host_env)
    if host:
        return f"{host.rstrip('/')}{path}"
    return default.rstrip("/")
