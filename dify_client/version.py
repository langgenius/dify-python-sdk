"""What this SDK calls itself, and what it says it is.

Dify logs the ``User-Agent`` of everything that talks to it, and every answer
it gives carries ``X-Version`` back. Until this module existed the SDK sent
``python-httpx/0.28.1`` — so a Dify operator looking at a misbehaving client
could tell it was written in Python and nothing else, and a bug report from a
user carried no version of either side.
"""

from __future__ import annotations

import platform
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed

import httpx

__all__ = ["UNKNOWN_VERSION", "USER_AGENT", "__version__", "user_agent"]

#: Stands in when the package is running from a source tree that was never
#: installed, so there is no distribution metadata to read. Deliberately not a
#: hard-coded number: one kept in two places drifts, and the wrong version in a
#: User-Agent is worse than an obvious placeholder.
UNKNOWN_VERSION = "0+unknown"

try:
    __version__ = _installed("dify-client")
except PackageNotFoundError:  # pragma: no cover - a bare source checkout
    __version__ = UNKNOWN_VERSION


#: What this SDK sends unless a caller has set a User-Agent of their own.
USER_AGENT = (
    f"dify-client/{__version__} "
    f"python/{platform.python_version()} "
    f"httpx/{httpx.__version__}"
)


def user_agent(existing: str | None = None) -> str:
    """The User-Agent to send, given whatever the HTTP client already had.

    A caller who passes their own ``http_client`` with a ``User-Agent`` set has
    said something about who they are, and keeps it — this appends rather than
    replaces, the way a library is supposed to. httpx's own default is not
    somebody's choice, so it is replaced outright.
    """
    if not existing or existing.startswith("python-httpx/"):
        return USER_AGENT
    if USER_AGENT in existing:
        return existing
    return f"{existing} {USER_AGENT}"
