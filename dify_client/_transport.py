"""The HTTP layer every client speaks through.

Not part of the public API: it holds the connection, the credential, retries
and error handling, and nothing about what Dify can do. The resources in
:mod:`dify_client.resources` are what name the operations.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

from .base_client import (
    BaseClientMixin,
    check_timeout,
    raise_for_error,
    request_headers,
    scoped_timeout,
    strip_none_params,
    timeout_override,
)
from .secrets import ApiKeyInput


def _describe(files: Dict[str, Any]) -> dict[str, str]:
    """What was uploaded, without what was in it."""
    described = {}
    for field, part in files.items():
        name, content = (part + (None,))[:2] if isinstance(part, tuple) else ("", part)
        size = len(content) if isinstance(content, (bytes, bytearray)) else "?"
        described[field] = f"{name or field} ({size} bytes)"
    return described


class Transport(BaseClientMixin):
    """Connection, credential, retries and errors. Nothing else.

    Every client in this SDK is one of these underneath. It is deliberately
    anonymous: it knows how to send a request to Dify, not what to ask for.
    """

    def __init__(
        self,
        api_key: ApiKeyInput = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        enable_logging: bool = False,
        http_client: httpx.Client | None = None,
    ):
        """Initialize the Dify client.

        Args:
            api_key: Your Dify API key, or a callable returning one. Left out,
                it is read from the ``DIFY_API_KEY`` environment variable.
            base_url: Base URL for the Dify API. Left out, it is read from
                ``DIFY_API_BASE_URL``, then defaults to Dify Cloud.
            timeout: Request timeout in seconds (default: 60.0). Not
                accepted alongside ``http_client``, which has its own — the
                one passed here would have been ignored.
            max_retries: Maximum number of retry attempts (default: 3)
            retry_delay: Delay between retries in seconds (default: 1.0)
            enable_logging: Whether to enable request logging (default: True)
            http_client: Your own ``httpx.Client``, for a proxy, a corporate
                TLS bundle, a shared connection pool, or a transport that never
                leaves the test process. Its ``base_url`` is used as given, so
                set it to the Service API root. ``DifyManagement`` and
                ``OpenApiClient`` take the same argument.
        """
        resolved_timeout = check_timeout(timeout, http_client)
        BaseClientMixin.__init__(
            self,
            api_key,
            base_url,
            resolved_timeout,
            max_retries,
            retry_delay,
            enable_logging,
        )

        self._client = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(resolved_timeout, connect=5.0),
        )

    def with_timeout(self, seconds: float | httpx.Timeout | None):
        """Give every call inside the block a different timeout.

        One number cannot serve a client that both reads a conversation and
        waits out a twenty-minute workflow, and threading a ``timeout=``
        argument through every method would put it on eighty signatures for
        the sake of two::

            with app.with_timeout(600):
                run = app.workflows.runs.create(inputs)

        Scoped to the calling thread or task, so raising it for one long call
        does not raise it for everything else in flight.
        """
        return scoped_timeout(seconds)

    def __enter__(self):
        """Support context manager protocol."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Clean up resources when exiting context."""
        self.close()

    def close(self):
        """Close the HTTP client and release resources."""
        if hasattr(self, "_client"):
            self._client.close()

    def _send_request(
        self,
        method: str,
        endpoint: str,
        json: Dict[str, Any] | None = None,
        params: Dict[str, Any] | None = None,
        stream: bool = False,
        **kwargs,
    ):
        """Send an HTTP request to the Dify API with retry logic.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE)
            endpoint: API endpoint path
            json: JSON request body
            params: Query parameters
            stream: Whether to stream the response
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

        def make_request():
            """Inner function to perform the actual HTTP request."""
            if self.enable_logging:
                self.logger.info(f"Sending {method} request to {endpoint}")
                if self.logger.isEnabledFor(logging.DEBUG):
                    if json:
                        self.logger.debug(f"Request body: {json}")
                    if params:
                        self.logger.debug(f"Request params: {params}")

            # httpx.Client prepends base_url for both paths below.
            if stream:
                # Hand back a response whose body has not been read, so the
                # caller sees events as Dify emits them. `.request()` reads to
                # completion first, which meant `response_mode="streaming"`
                # waited for the whole generation and only then replayed it.
                request = self._client.build_request(
                    method,
                    endpoint,
                    json=json,
                    params=params,
                    headers=headers,
                    **kwargs,
                )
                response = self._client.send(request, stream=True)
            else:
                response = self._client.request(
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

        request_context = f"{method} {endpoint}"
        response = self._retry_request(make_request, request_context, method=method)

        # Error responses do not retry. For a stream this reads the body first,
        # otherwise the reason would be unreachable.
        self._handle_error_response(response)

        return response

    def _handle_error_response(self, response, is_upload_request: bool = False) -> None:
        """Raise the exception that names this error, if there is one."""
        raise_for_error(
            response,
            is_upload_request=is_upload_request,
            logger=self.logger if self.enable_logging else None,
        )

    def _send_request_with_files(
        self, method: str, endpoint: str, data: dict, files: dict
    ):
        """Send an HTTP request with file uploads.

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
        upload: dict[str, Any] = {}
        scoped = timeout_override()
        if scoped is not None:
            upload["timeout"] = scoped

        # Log file upload request if logging is enabled
        if self.enable_logging:
            self.logger.info(f"Sending {method} file upload request to {endpoint}")
            self.logger.debug(f"Form data: {data}")
            # Names and sizes, never the bytes: a debug log is not the place
            # for the contents of somebody's upload.
            self.logger.debug(f"Files: {_describe(files)}")

        response = self._client.request(
            method,
            endpoint,
            data=data,
            headers=headers,
            files=files,
            **upload,
        )

        # Log response if logging is enabled
        if self.enable_logging:
            self.logger.info(f"Received file upload response: {response.status_code}")

        # Handle error responses
        self._handle_error_response(response, is_upload_request=True)

        return response
