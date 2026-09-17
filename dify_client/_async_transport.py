"""The async HTTP layer, matching :mod:`dify_client._transport`.

Not part of the public API. Retries, error handling and streaming behave
exactly as the sync side does — that they once did not was a bug.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict

import httpx

from .base_client import (
    check_timeout,
    is_safe_to_retry,
    raise_for_error,
    rate_limit_wait,
    request_headers,
    retry_delay_for,
    scoped_timeout,
    strip_none_params,
    timeout_override,
)
from .exceptions import DifyClientError, NetworkError, RequestTimeout, ValidationError
from .secrets import ApiKeyInput, resolve_api_key, resolve_base_url


class AsyncTransport:
    """Connection, credential, retries and errors. Nothing else."""

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
        http_client: httpx.AsyncClient | None = None,
    ):
        """Initialize the async Dify client.

        Args:
            api_key: Your Dify API key, or a callable returning one. Left out,
                it is read from the ``DIFY_API_KEY`` environment variable.
                A callable is resolved per request, so a rotating key from a
                secret store never has to be baked into the client.
            base_url: Base URL for the Dify API. Left out, it is read from
                ``DIFY_API_BASE_URL``, then defaults to Dify Cloud.
            timeout: Request timeout in seconds (default: 60.0). Not
                accepted alongside ``http_client``, which has its own.
            max_retries: How many times to retry a network or timeout failure.
            retry_delay: Seconds before the first retry; doubles after each.
            enable_logging: Log requests, responses and errors.
            http_client: Your own ``httpx.AsyncClient``, for a proxy, a
                corporate TLS bundle, a shared connection pool, or a transport
                that never leaves the test process. Its ``base_url`` is used as
                given, so set it to the Service API root.
        """
        try:
            self._api_key = resolve_api_key(api_key)
        except ValueError as error:
            raise ValidationError(str(error)) from error

        resolved_timeout = check_timeout(timeout, http_client)
        self.base_url = resolve_base_url(base_url, self.DEFAULT_BASE_URL)
        self.timeout = resolved_timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.enable_logging = enable_logging
        #: End-user identifier used when a call does not name one.
        self.default_user = ""
        self.logger = logging.getLogger(f"dify_client.{type(self).__name__.lower()}")
        self._client = http_client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(resolved_timeout, connect=5.0),
        )

    @property
    def api_key(self) -> str:
        """The API key itself, resolved from the provider if there is one."""
        return self._api_key.reveal()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self.base_url!r}, "
            f"api_key={self._api_key!r})"
        )

    def with_timeout(self, seconds: float | httpx.Timeout | None):
        """Give every call inside the block a different timeout.

        The same scope the sync client has, and it is per task: two coroutines
        on one loop do not share a deadline.
        """
        return scoped_timeout(seconds)

    async def __aenter__(self):
        """Support async context manager protocol."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Clean up resources when exiting async context."""
        await self.aclose()

    async def aclose(self):
        """Close the async HTTP client and release resources."""
        if hasattr(self, "_client"):
            await self._client.aclose()

    async def _retry_request(self, make_request, context: str, method: str = "GET"):
        """Retry network and timeout failures, the way the sync client does.

        A failure after the request was sent is only retried for an idempotent
        method — see :func:`~dify_client.base_client.is_safe_to_retry`.
        """
        for attempt in range(self.max_retries + 1):
            try:
                response = await make_request()
                wait = rate_limit_wait(
                    response, attempt, self.max_retries, self.retry_delay
                )
                if wait is None:
                    return response
                await response.aclose()
                if self.enable_logging:
                    self.logger.warning(
                        f"Rate limited {context}; waiting {wait:g}s "
                        f"(attempt {attempt + 1}/{self.max_retries + 1})"
                    )
                await asyncio.sleep(wait)
                continue
            except (httpx.NetworkError, httpx.TimeoutException) as error:
                retryable = is_safe_to_retry(error, method)
                if attempt >= self.max_retries or not retryable:
                    detail = (
                        " and was not retried: it may already have been acted on"
                        if not retryable
                        else f" after {self.max_retries} retries"
                    )
                    if self.enable_logging:
                        self.logger.error(f"Request failed {context}{detail}: {error}")
                    if isinstance(error, httpx.TimeoutException):
                        raise RequestTimeout(
                            f"Request timed out{detail} {context}"
                        ) from error
                    raise NetworkError(
                        f"Network error{detail} {context}: {error}"
                    ) from error
                delay = retry_delay_for(attempt, self.retry_delay)
                if self.enable_logging:
                    self.logger.warning(
                        f"Request failed {context} (attempt {attempt + 1}/"
                        f"{self.max_retries + 1}): {error}. "
                        f"Retrying in {delay:.2f} seconds..."
                    )
                await asyncio.sleep(delay)
        raise DifyClientError("Request failed after retries")

    async def _send_request(
        self,
        method: str,
        endpoint: str,
        json: Dict | None = None,
        params: Dict | None = None,
        stream: bool = False,
        **kwargs,
    ):
        """Send an async HTTP request to the Dify API.

        Retries, error handling and streaming match the sync client exactly.
        Until this was fixed the async client returned a 401 rather than
        raising, so a bad key looked like a successful call.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE)
            endpoint: API endpoint path
            json: JSON request body
            params: Query parameters
            stream: Leave the body unread so events arrive as Dify emits them
            **kwargs: Additional arguments to pass to httpx.request

        Returns:
            httpx.Response object
        """
        params = strip_none_params(params)

        headers = request_headers(
            self.api_key, existing_agent=self._client.headers.get("User-Agent")
        )
        scoped = timeout_override()
        if scoped is not None:
            kwargs.setdefault("timeout", scoped)

        async def make_request():
            if self.enable_logging:
                self.logger.info(f"Sending {method} request to {endpoint}")
            if stream:
                request = self._client.build_request(
                    method,
                    endpoint,
                    json=json,
                    params=params,
                    headers=headers,
                    **kwargs,
                )
                response = await self._client.send(request, stream=True)
            else:
                response = await self._client.request(
                    method,
                    endpoint,
                    json=json,
                    params=params,
                    headers=headers,
                    **kwargs,
                )
            if self.enable_logging:
                self.logger.info(f"Received response: {response.status_code}")
            return response

        response = await self._retry_request(
            make_request, f"{method} {endpoint}", method=method
        )
        await self._handle_error_response(response)
        return response

    async def _handle_error_response(
        self, response: httpx.Response, is_upload_request: bool = False
    ) -> None:
        """Raise the exception that names this error, if there is one."""
        if response.status_code >= 400 and not response.is_closed:
            # raise_for_error would otherwise hit ResponseNotRead on a stream.
            await response.aread()
        raise_for_error(
            response,
            is_upload_request=is_upload_request,
            logger=self.logger if self.enable_logging else None,
        )

    async def _send_request_with_files(
        self, method: str, endpoint: str, data: dict, files: dict
    ):
        """Send an async HTTP request with file uploads.

        Args:
            method: HTTP method (POST, PUT, etc.)
            endpoint: API endpoint path
            data: Form data
            files: Files to upload

        Returns:
            httpx.Response object
        """
        headers = request_headers(
            self.api_key, existing_agent=self._client.headers.get("User-Agent")
        )
        # The multipart boundary is httpx's to set, not ours.
        headers.pop("Content-Type", None)
        upload: dict = {}
        scoped = timeout_override()
        if scoped is not None:
            upload["timeout"] = scoped

        response = await self._client.request(
            method,
            endpoint,
            data=data,
            headers=headers,
            files=files,
            **upload,
        )

        await self._handle_error_response(response, is_upload_request=True)
        return response
