"""Base client with common functionality for both sync and async clients."""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Dict

try:
    # Python 3.10+
    from typing import ParamSpec
except ImportError:
    # Python < 3.10
    from typing_extensions import ParamSpec


import httpx

from .exceptions import (
    APIError,
    AuthenticationError,
    DifyClientError,
    FileUploadError,
    NetworkError,
    RateLimitError,
    RequestTimeout,
    ValidationError,
)
from .secrets import ApiKeyInput, resolve_api_key, resolve_base_url
from .version import user_agent

P = ParamSpec("P")

#: How long a request waits when nobody said otherwise.
DEFAULT_TIMEOUT = 60.0


def check_timeout(timeout: float | None, http_client: object | None) -> float:
    """The timeout to build a client with, or refuse a value that would be lost.

    An ``httpx`` client carries its own timeout, and a request made through
    one uses that — so ``timeout=`` beside ``http_client=`` was accepted,
    stored, and then ignored. Saying so is better than a number that does
    nothing: the caller who passed 5 seconds meant it.
    """
    if http_client is not None and timeout is not None:
        msg = (
            "timeout= and http_client= together: the timeout would be ignored, "
            "because a request made through your own client uses that client's "
            "timeout. Set it there instead — httpx.Client(timeout=…) — or use "
            "client.with_timeout(seconds) around the calls it should apply to."
        )
        raise ValidationError(msg)
    return DEFAULT_TIMEOUT if timeout is None else timeout


#: The timeout for calls made inside a ``with client.with_timeout(...)`` block.
#: A ContextVar rather than an attribute so that two threads, or two tasks on
#: one event loop, cannot change each other's deadline.
_SCOPED_TIMEOUT: ContextVar[float | httpx.Timeout | None] = ContextVar(
    "dify_scoped_timeout", default=None
)


@contextmanager
def scoped_timeout(
    seconds: float | httpx.Timeout | None,
) -> Iterator[None]:
    """Apply a timeout to every request made inside the block."""
    token = _SCOPED_TIMEOUT.set(seconds)
    try:
        yield
    finally:
        _SCOPED_TIMEOUT.reset(token)


def timeout_override() -> float | httpx.Timeout | None:
    """The timeout in force for this call, or None to use the client's."""
    return _SCOPED_TIMEOUT.get()


def request_headers(
    api_key: str, *, existing_agent: str | None = None
) -> Dict[str, str]:
    """The headers every JSON request carries."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": user_agent(existing_agent),
    }


class BaseClientMixin:
    """Mixin class providing common functionality for Dify clients."""

    #: Where the API base URL comes from when none is passed.
    DEFAULT_BASE_URL = "https://api.dify.ai/v1"

    def __init__(
        self,
        api_key: ApiKeyInput = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        enable_logging: bool = False,
    ):
        """Initialize the base client.

        Args:
            api_key: Your Dify API key, or a callable returning one. Left out,
                it is read from the ``DIFY_API_KEY`` environment variable.
                A callable is resolved per request, so a rotating key from a
                secret store never has to be baked into the client.
            base_url: Base URL for the Dify API. Left out, it is read from
                ``DIFY_API_BASE_URL``, then defaults to Dify Cloud.
            timeout: Request timeout in seconds. Left out, 60. Cannot be
                combined with ``http_client``, which carries its own.
            max_retries: Maximum number of retry attempts
            retry_delay: Delay between retries in seconds
            enable_logging: Enable detailed logging
        """
        try:
            self._api_key = resolve_api_key(api_key)
        except ValueError as error:
            raise ValidationError(str(error)) from error

        self.base_url = resolve_base_url(base_url, self.DEFAULT_BASE_URL)
        self.timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.enable_logging = enable_logging
        #: End-user identifier used when a call does not name one.
        self.default_user = ""

        # Setup logging
        self.logger = logging.getLogger(
            f"dify_client.{self.__class__.__name__.lower()}"
        )
        if enable_logging and not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
                )
            )
            # The handler prints the summary lines; the logger passes
            # everything. Capping the *logger* at INFO was the same as
            # deleting the debug lines — bodies, parameters, uploads — for any
            # application that went looking for them with a handler of its
            # own. Now `logging.getLogger("dify_client").setLevel(DEBUG)` is
            # not needed and a DEBUG handler simply receives them.
            handler.setLevel(logging.INFO)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.DEBUG)
        self.enable_logging = enable_logging

    @property
    def api_key(self) -> str:
        """The API key itself, resolved from the provider if there is one."""
        return self._api_key.reveal()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self.base_url!r}, "
            f"api_key={self._api_key!r})"
        )

    def _retry_request(
        self,
        request_func: Callable[P, httpx.Response],
        request_context: str | None = None,
        method: str = "GET",
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> httpx.Response:
        """Retry a request with exponential backoff.

        Args:
            request_func: Function that performs the HTTP request
            request_context: Context description for logging (e.g., "GET /v1/messages")
            *args: Positional arguments to pass to request_func
            **kwargs: Keyword arguments to pass to request_func

        Returns:
            httpx.Response: Successful response

        Raises:
            NetworkError: On network failures after retries
            RequestTimeout: On timeout failures after retries
            APIError: On API errors (4xx/5xx responses)
            DifyClientError: On unexpected failures

        A failure after the request was sent is only retried for an idempotent
        method — see :func:`is_safe_to_retry`. Retrying a POST that Dify may
        already have acted on can bill the same run several times.
        """
        last_exception = None

        for attempt in range(self.max_retries + 1):
            try:
                response = request_func(*args, **kwargs)
                wait = rate_limit_wait(
                    response, attempt, self.max_retries, self.retry_delay
                )
                if wait is None:
                    return response  # Let caller handle response processing
                response.close()
                self.logger.warning(
                    f"Rate limited {request_context or ''}; waiting {wait:g}s "
                    f"(attempt {attempt + 1}/{self.max_retries + 1})"
                )
                time.sleep(wait)
                continue

            except (httpx.NetworkError, httpx.TimeoutException) as e:
                last_exception = e
                context_msg = f" {request_context}" if request_context else ""

                if attempt < self.max_retries and is_safe_to_retry(e, method):
                    delay = retry_delay_for(attempt, self.retry_delay)
                    self.logger.warning(
                        f"Request failed{context_msg} (attempt {attempt + 1}/{self.max_retries + 1}): {e}. "
                        f"Retrying in {delay:.2f} seconds..."
                    )
                    time.sleep(delay)
                else:
                    unsafe = not is_safe_to_retry(e, method)
                    detail = (
                        " and was not retried: it may already have been acted on"
                        if unsafe
                        else f" after {self.max_retries} retries"
                    )
                    self.logger.error(f"Request failed{context_msg}{detail}: {e}")
                    if isinstance(e, httpx.TimeoutException):
                        raise RequestTimeout(
                            f"Request timed out{detail}{context_msg}"
                        ) from e
                    raise NetworkError(
                        f"Network error{detail}{context_msg}: {e}"
                    ) from e

        if last_exception:
            raise last_exception
        raise DifyClientError("Request failed after retries")


def raise_for_error(
    response: httpx.Response,
    *,
    is_upload_request: bool = False,
    logger: logging.Logger | None = None,
) -> None:
    """Turn an error response into the exception that names it.

    Both clients call this, so an async failure raises exactly what the same
    sync call would. The async client used to return the 401 instead, leaving
    the caller to notice on their own.
    """
    if response.status_code < 400:
        return

    # A streamed response has no body yet, and .json() would raise
    # ResponseNotRead instead of telling anyone what went wrong.
    if not response.is_closed:
        response.read()

    try:
        error_data = response.json()
        message = error_data.get("message", f"HTTP {response.status_code}")
    except (ValueError, KeyError, AttributeError):
        message = f"HTTP {response.status_code}"
        error_data = None

    if logger is not None:
        logger.error(f"API error: {response.status_code} - {message}")

    headers = response.headers

    if response.status_code == 401:
        raise AuthenticationError(message, response.status_code, error_data, headers)
    if response.status_code == 429:
        hinted = retry_after_seconds(headers.get("Retry-After"))
        raise RateLimitError(
            message,
            None if hinted is None else int(hinted),
            error_data,
            headers,
        )
    if response.status_code == 422:
        raise ValidationError(message, response.status_code, error_data, headers)
    if response.status_code == 400:
        url = str(getattr(response, "url", "") or "").lower()
        if is_upload_request or "upload" in url or "files" in url:
            raise FileUploadError(message, response.status_code, error_data, headers)
    raise APIError(message, response.status_code, error_data, headers)


#: The longest this client will sit waiting out a 429, however long Dify asks
#: for. A server under maintenance can answer `Retry-After: 3600`, and a call
#: that blocks for an hour is indistinguishable from one that hung.
MAX_RATE_LIMIT_WAIT = 60.0


def retry_after_seconds(header: str | None) -> float | None:
    """Read ``Retry-After``, which HTTP allows to be a delay or a date."""
    if not header:
        return None
    try:
        return max(0.0, float(int(header)))
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    import datetime as _datetime

    now = _datetime.datetime.now(tz=when.tzinfo)
    return max(0.0, (when - now).total_seconds())


def rate_limit_wait(
    response: httpx.Response, attempt: int, max_retries: int, base_delay: float
) -> float | None:
    """How long to wait before repeating a request Dify rate-limited.

    None when it should not be repeated at all — attempts are used up, or the
    server asked for longer than anyone should block for.

    Repeating is safe for any method, unlike a retry after a timeout: a 429
    means Dify refused the request, so there is nothing it might already have
    done. Without this a burst of calls raised RateLimitError and left the
    caller to write the sleep loop, which every caller then wrote differently.
    """
    if response.status_code != 429 or attempt >= max_retries:
        return None
    hinted = retry_after_seconds(response.headers.get("Retry-After"))
    delay = hinted if hinted is not None else retry_delay_for(attempt, base_delay)
    if delay > MAX_RATE_LIMIT_WAIT:
        return None
    return delay


def strip_none_params(params: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Drop query parameters set to None.

    httpx renders ``{"limit": None}`` as ``?limit=``, and Dify's typed query
    models reject ``""`` where they want an int — a plain
    ``get_conversations(user)`` answered 422 because of it. Request bodies are
    left alone: a null there is a value, e.g. ``rating=None`` revokes feedback.
    """
    if not params:
        return params
    return {k: v for k, v in params.items() if v is not None}


def retry_delay_for(attempt: int, base_delay: float) -> float:
    """Exponential backoff, shared so both clients wait the same way."""
    return base_delay * (2**attempt)


#: Methods HTTP defines as idempotent: repeating one has the same effect as
#: making it once, so a retry cannot duplicate anything.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

#: Failures that happen before the request reaches the server. Retrying these
#: is always safe — the server never saw the first attempt.
BEFORE_SEND = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def is_safe_to_retry(error: Exception, method: str) -> bool:
    """Whether repeating this request can duplicate work already done.

    A `ConnectError` means nothing was sent, so a retry is free. A `ReadTimeout`
    means the request *was* sent and no answer came back — Dify may well have
    run the workflow, charged for the model calls and fired the tool nodes. The
    SDK used to retry that three more times, so one timed-out
    `POST /workflows/run` could bill four runs.

    Idempotent methods are retried either way, because repeating one is
    defined to have the same effect as making it once.
    """
    if isinstance(error, BEFORE_SEND):
        return True
    return method.upper() in IDEMPOTENT_METHODS
