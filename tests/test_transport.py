"""The HTTP layer: retries, errors, streaming, and the httpx move.

Everything here used to be spread across the old client classes. The behaviour
belongs to the transport, not to any one of them, which is why the transport is
now its own thing and these are one file.
"""

import asyncio
import datetime
import logging
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from dify_client._async_transport import AsyncTransport
from dify_client._transport import Transport
from dify_client.base_client import IDEMPOTENT_METHODS, is_safe_to_retry
from dify_client.exceptions import (
    APIError,
    AuthenticationError,
    DifyClientError,
    FileUploadError,
    NetworkError,
    RateLimitError,
    RequestTimeout,
    ValidationError,
)

REQUEST = httpx.Request("GET", "https://api.dify.ai/v1/info")


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def transport(response=None, *, request=None, **kwargs):
    # No real waiting in a unit test: a 429 is now waited out rather than
    # raised on sight, and a test that meant to check the exception would
    # otherwise sit through the backoff.
    kwargs.setdefault("retry_delay", 0)
    client = Transport("k", base_url="https://api.dify.ai/v1", **kwargs)
    client._client = Mock()
    client._client.request = request or Mock(return_value=response)
    client._client.build_request = Mock(return_value=Mock())
    client._client.send = Mock(return_value=response)
    return client


class TestItSpeaksHttpx:
    def test_the_connection_is_an_httpx_client(self):
        assert isinstance(Transport("k", base_url="https://x/v1")._client, httpx.Client)

    def test_the_async_one_is_an_async_client(self):
        assert isinstance(
            AsyncTransport("k", base_url="https://x/v1")._client, httpx.AsyncClient
        )

    def test_it_closes_on_the_way_out_of_a_with_block(self):
        client = Transport("k", base_url="https://x/v1")
        with client:
            pass
        assert client._client.is_closed

    def test_the_key_signs_every_request(self):
        sent = []
        client = transport(
            httpx.Response(200, json={}, request=REQUEST),
            request=lambda *a, **kw: sent.append(kw["headers"])
            or httpx.Response(200, json={}, request=REQUEST),
        )
        client._send_request("GET", "/info")
        assert sent[0]["Authorization"] == "Bearer k"

    def test_a_rotating_key_is_resolved_per_request(self):
        keys = iter(["app-one", "app-two"])
        sent = []
        client = Transport(lambda: next(keys), base_url="https://x/v1")
        client._client = Mock()
        client._client.request = lambda *a, **kw: sent.append(
            kw["headers"]["Authorization"]
        ) or httpx.Response(200, json={}, request=REQUEST)
        client._send_request("GET", "/info")
        client._send_request("GET", "/info")
        assert sent == ["Bearer app-one", "Bearer app-two"]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthenticationError),
        (422, ValidationError),
        (429, RateLimitError),
        (404, APIError),
        (500, APIError),
    ],
    ids=lambda v: str(v),
)
class TestErrorsAreRaised:
    def _response(self, status):
        return httpx.Response(status, json={"message": "nope"}, request=REQUEST)

    def test_sync(self, status, error):
        with pytest.raises(error):
            transport(self._response(status))._send_request("GET", "/info")

    def test_async_raises_the_same(self, status, error):
        """It used to return the 401 instead, so a bad key looked fine."""
        client = AsyncTransport("k", base_url="https://x/v1", retry_delay=0)
        client._client = Mock()
        client._client.request = AsyncMock(return_value=self._response(status))

        async def call():
            return await client._send_request("GET", "/info")

        with pytest.raises(error):
            asyncio.run(call())


class TestErrorDetail:
    def test_difys_message_comes_through(self):
        response = httpx.Response(401, json={"message": "bad key"}, request=REQUEST)
        with pytest.raises(AuthenticationError, match="bad key"):
            transport(response)._send_request("GET", "/info")

    def test_a_rate_limit_carries_the_wait_as_an_int(self):
        response = httpx.Response(
            429,
            json={"message": "slow"},
            headers={"Retry-After": "30"},
            request=REQUEST,
        )
        with pytest.raises(RateLimitError) as caught:
            transport(response, max_retries=0)._send_request("GET", "/info")
        assert caught.value.retry_after == 30

    def test_an_http_date_retry_after_is_read_as_a_delay(self):
        """HTTP allows either spelling. The date used to be dropped on the
        floor, so a server that answered one looked like it had said nothing."""
        from email.utils import format_datetime

        when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=90
        )
        response = httpx.Response(
            429,
            json={"message": "slow"},
            headers={"Retry-After": format_datetime(when)},
            request=REQUEST,
        )
        with pytest.raises(RateLimitError) as caught:
            transport(response, max_retries=0)._send_request("GET", "/info")
        assert 60 <= caught.value.retry_after <= 90

    def test_nonsense_in_retry_after_is_dropped(self):
        response = httpx.Response(
            429,
            json={"message": "slow"},
            headers={"Retry-After": "soon"},
            request=REQUEST,
        )
        with pytest.raises(RateLimitError) as caught:
            transport(response, max_retries=0)._send_request("GET", "/info")
        assert caught.value.retry_after is None

    def test_an_upload_failure_is_named_as_one(self):
        response = httpx.Response(
            400,
            json={"message": "too large"},
            request=httpx.Request("POST", "https://x/v1/files/upload"),
        )
        client = transport(response)
        with pytest.raises(FileUploadError, match="too large"):
            client._handle_error_response(response, is_upload_request=True)

    def test_a_body_that_is_not_json_still_raises(self):
        response = httpx.Response(500, text="<html>oops</html>", request=REQUEST)
        with pytest.raises(APIError, match="HTTP 500"):
            transport(response)._send_request("GET", "/info")


class TestRetries:
    def _run(self, error, method="GET", path="/info", **kwargs):
        """Returns (attempts, raised) so a test can assert on either."""
        seen = {"n": 0}

        def request(*_a, **_kw):
            seen["n"] += 1
            raise error

        client = transport(request=request, retry_delay=0, **kwargs)
        try:
            client._send_request(method, path)
        except DifyClientError as raised:
            return seen["n"], raised
        raise AssertionError("the transport swallowed the failure")

    def _attempts(self, error, method="GET", path="/info", **kwargs):
        return self._run(error, method, path, **kwargs)[0]

    def test_a_connection_failure_is_retried(self):
        assert self._attempts(httpx.ConnectError("x")) == 4

    def test_it_gives_up_and_names_the_failure(self):
        assert isinstance(self._run(httpx.ConnectError("x"))[1], NetworkError)

    def test_a_timeout_is_named_as_one(self):
        assert isinstance(self._run(httpx.ConnectTimeout("x"))[1], RequestTimeout)

    def test_the_count_is_configurable(self):
        assert self._attempts(httpx.ConnectError("x"), max_retries=1) == 2

    def test_a_post_is_not_retried_once_it_has_been_sent(self):
        """A ReadTimeout means Dify may have run it, and charged for it."""
        assert self._attempts(httpx.ReadTimeout("x"), "POST", "/workflows/run") == 1

    def test_a_post_is_retried_when_it_never_left(self):
        assert self._attempts(httpx.ConnectError("x"), "POST", "/workflows/run") == 4

    def test_a_get_is_retried_either_way(self):
        assert self._attempts(httpx.ReadTimeout("x"), "GET", "/info") == 4

    def test_the_message_says_the_call_may_have_landed(self):
        _, raised = self._run(httpx.ReadTimeout("x"), "POST", "/workflows/run")
        assert "may already have been acted on" in str(raised)

    @pytest.mark.parametrize(
        ("error", "method", "safe"),
        [
            (httpx.ConnectError("x"), "POST", True),
            (httpx.ConnectTimeout("x"), "POST", True),
            (httpx.PoolTimeout("x"), "POST", True),
            (httpx.ReadTimeout("x"), "POST", False),
            (httpx.WriteError("x"), "PATCH", False),
            (httpx.ReadTimeout("x"), "GET", True),
            (httpx.ReadTimeout("x"), "DELETE", True),
        ],
        ids=lambda v: str(v)[:24],
    )
    def test_the_rule(self, error, method, safe):
        assert is_safe_to_retry(error, method) is safe

    def test_the_unsafe_verbs_are_the_ones_that_change_things(self):
        assert "POST" not in IDEMPOTENT_METHODS
        assert "PATCH" not in IDEMPOTENT_METHODS
        assert {"GET", "PUT", "DELETE", "HEAD", "OPTIONS"} == IDEMPOTENT_METHODS

    def test_the_async_transport_retries_too(self):
        """It used to give up after the first failure."""
        seen = {"n": 0}

        async def request(*_a, **_kw):
            seen["n"] += 1
            raise httpx.ConnectError("x")

        client = AsyncTransport("k", base_url="https://x/v1", retry_delay=0)
        client._client = Mock()
        client._client.request = request

        async def call():
            await client._send_request("GET", "/info")

        with pytest.raises(NetworkError):
            asyncio.run(call())
        assert seen["n"] == 4

    def test_backoff_doubles(self):
        from dify_client.base_client import retry_delay_for

        assert [retry_delay_for(n, 1.0) for n in range(4)] == [1.0, 2.0, 4.0, 8.0]


class TestStreaming:
    def test_a_streaming_request_asks_httpx_not_to_read_the_body(self):
        client = transport(Mock(status_code=200, is_closed=True))
        client._send_request("POST", "/workflows/run", {"a": 1}, stream=True)
        assert client._client.send.call_args.kwargs["stream"] is True

    def test_a_buffered_request_does_not_go_through_send(self):
        client = transport(httpx.Response(200, json={}, request=REQUEST))
        client._send_request("POST", "/workflows/run", {"a": 1})
        client._client.request.assert_called_once()
        client._client.send.assert_not_called()

    def test_a_streamed_error_is_read_before_it_is_raised(self):
        """Otherwise .json() raises ResponseNotRead and hides the reason."""
        response = httpx.Response(
            401,
            json={"message": "bad key"},
            request=httpx.Request("POST", "https://x/v1/workflows/run"),
        )
        client = transport(response)
        client._client.send = Mock(return_value=response)
        with pytest.raises(AuthenticationError, match="bad key"):
            client._send_request("POST", "/workflows/run", {}, stream=True)


class TestQueryParameters:
    def test_a_none_value_is_dropped_rather_than_sent_empty(self):
        """httpx renders None as `?limit=`, which Dify's int fields reject."""
        sent = []
        client = transport(
            request=lambda *a, **kw: sent.append(kw["params"])
            or httpx.Response(200, json={}, request=REQUEST)
        )
        client._send_request(
            "GET", "/conversations", params={"user": "u", "limit": None}
        )
        assert sent[0] == {"user": "u"}

    def test_a_none_in_the_body_is_kept_because_it_means_something(self):
        """rating=None revokes feedback; dropping it changes the call."""
        sent = []
        client = transport(
            request=lambda *a, **kw: sent.append(kw["json"])
            or httpx.Response(200, json={}, request=REQUEST)
        )
        client._send_request("POST", "/messages/m/feedbacks", {"rating": None})
        assert sent[0] == {"rating": None}

    def test_httpx_really_would_have_sent_an_empty_string(self):
        url = httpx.Request("GET", "https://x/c", params={"limit": None}).url
        assert str(url).endswith("limit=")


class TestNoInventedLimits:
    """The transport used to reject request bodies Dify would have accepted:
    a string over 10,000 characters, a list over 1,000 items, a dict over 100
    keys. A knowledge-base document longer than a few pages was unpostable."""

    def _sent(self, body):
        sent = []
        client = transport(
            request=lambda *a, **kw: sent.append(kw["json"])
            or httpx.Response(200, json={}, request=REQUEST)
        )
        client._send_request("POST", "/anything", body)
        return sent[0]

    def test_a_long_string_goes_through(self):
        assert len(self._sent({"text": "x" * 50_000})["text"]) == 50_000

    def test_a_long_list_goes_through(self):
        assert len(self._sent({"items": list(range(5_000))})["items"]) == 5_000

    def test_a_wide_dict_goes_through(self):
        assert (
            len(self._sent({"inputs": {str(n): n for n in range(500)}})["inputs"])
            == 500
        )

    def test_an_empty_string_is_a_value_not_an_error(self):
        assert self._sent({"keyword": ""})["keyword"] == ""
