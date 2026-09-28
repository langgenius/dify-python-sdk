"""Exceptions the Dify client raises.

The hierarchy mirrors what went wrong, so a caller can be as broad or as narrow
as they want::

    DifyClientError                 everything below
    ├── APIError                    Dify answered, and it was an error
    │   ├── AuthenticationError     401 — the key is wrong or expired
    │   ├── RateLimitError          429 — too many requests
    │   ├── ValidationError         422, and bad arguments caught before sending
    │   └── FileUploadError         an upload Dify would not take
    └── TransportError              the request never got an answer
        ├── NetworkError            the connection failed
        └── RequestTimeout          it ran past the timeout

`except APIError` used to miss every one of the four beneath it, because the
hierarchy was flat — each inherited DifyClientError directly, so catching the
class that names HTTP errors caught only the ones with no better name.
"""

from collections.abc import Mapping
from typing import Any, Dict


class DifyClientError(Exception):
    """Base exception for all Dify client errors.

    Carries which Dify answered, when Dify answered at all. Every response it
    sends — errors included — has ``X-Version`` and ``X-Env`` on it, and the
    first question about a failing call is always which server and which
    version. Asking the user afterwards rarely works: by then the traceback is
    all that is left, so the version goes in the message.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response: Dict[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        #: The version of the Dify that answered, from ``X-Version``.
        self.server_version = str((headers or {}).get("X-Version") or "")
        #: ``PRODUCTION`` or ``TESTING``, from ``X-Env``.
        self.server_env = str((headers or {}).get("X-Env") or "")
        #: Dify's own id for this request, when it is tracing.
        self.trace_id = str((headers or {}).get("X-Trace-Id") or "")

        super().__init__(
            f"{message} [Dify {self.server_version}]"
            if self.server_version
            else message
        )
        #: What went wrong, without the server it went wrong on.
        self.message = message
        self.status_code = status_code
        self.response = response


class APIError(DifyClientError):
    """Dify answered, and the answer was an error."""


class AuthenticationError(APIError):
    """The API key was rejected — wrong, revoked or expired."""


class RateLimitError(APIError):
    """Too many requests. ``retry_after`` holds the server's hint, in seconds."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: int | None = None,
        response: Dict[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        super().__init__(message, 429, response, headers)
        self.retry_after = retry_after


class ValidationError(APIError):
    """The request was not acceptable.

    Raised for a 422 from Dify, and for arguments this client can tell are
    wrong before spending a request on them.
    """


class FileUploadError(APIError):
    """Dify would not take the uploaded file — too large, or the wrong type."""


class TransportError(DifyClientError):
    """The request never reached Dify, or never got an answer back."""


class NetworkError(TransportError):
    """The connection failed, and retrying did not help."""


class RequestTimeout(TransportError):
    """The request ran past its timeout, and retrying did not help.

    Named to stay out of the way of the builtin ``TimeoutError``. The old name
    is still importable and is an alias for this.
    """


#: The former name. Shadowing the builtin made `except TimeoutError` ambiguous:
#: whichever was imported last won, and the two are unrelated.
TimeoutError = RequestTimeout  # noqa: A001
