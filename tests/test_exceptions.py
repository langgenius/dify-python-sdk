"""The exception hierarchy, and what each level lets a caller catch.

It used to be flat: every class inherited DifyClientError directly, so
`except APIError` — the class whose name says "Dify returned an error" — caught
neither a 401 nor a 429 nor a 422. Callers had to either catch the root or
enumerate all five.
"""

import pytest

from dify_client import exceptions as ex
from dify_client.exceptions import (
    APIError,
    AuthenticationError,
    DifyClientError,
    FileUploadError,
    NetworkError,
    RateLimitError,
    RequestTimeout,
    TransportError,
    ValidationError,
)

HTTP_ERRORS = [AuthenticationError, RateLimitError, ValidationError, FileUploadError]
TRANSPORT_ERRORS = [NetworkError, RequestTimeout]


class TestCatchingBroadly:
    @pytest.mark.parametrize("error", HTTP_ERRORS, ids=lambda e: e.__name__)
    def test_api_error_catches_every_http_failure(self, error):
        with pytest.raises(APIError):
            raise error("boom")

    @pytest.mark.parametrize("error", TRANSPORT_ERRORS, ids=lambda e: e.__name__)
    def test_transport_error_catches_every_connection_failure(self, error):
        with pytest.raises(TransportError):
            raise error("boom")

    @pytest.mark.parametrize(
        "error",
        HTTP_ERRORS + TRANSPORT_ERRORS + [APIError, TransportError],
        ids=lambda e: e.__name__,
    )
    def test_the_root_still_catches_everything(self, error):
        with pytest.raises(DifyClientError):
            raise error("boom")

    def test_a_connection_failure_is_not_an_api_error(self):
        """Nothing came back, so there is no API answer to speak of."""
        assert not issubclass(NetworkError, APIError)
        assert not issubclass(RequestTimeout, APIError)


class TestCatchingNarrowly:
    def test_a_bad_key_is_distinguishable_from_any_other_error(self):
        with pytest.raises(AuthenticationError):
            raise AuthenticationError("bad key")
        assert not issubclass(ValidationError, AuthenticationError)

    def test_the_details_survive(self):
        error = APIError("not found", 404, {"code": "not_found"})
        assert (error.message, error.status_code, error.response["code"]) == (
            "not found",
            404,
            "not_found",
        )

    def test_a_rate_limit_carries_the_wait(self):
        error = RateLimitError("slow down", retry_after=30)
        assert error.retry_after == 30
        assert error.status_code == 429

    def test_a_rate_limit_with_no_hint_says_so(self):
        assert RateLimitError().retry_after is None


class TestNames:
    def test_the_builtin_timeout_is_no_longer_shadowed(self):
        """`except TimeoutError` used to depend on which import came last."""
        assert RequestTimeout is not TimeoutError
        assert not issubclass(RequestTimeout, TimeoutError)

    def test_the_old_name_still_imports(self):
        assert ex.TimeoutError is RequestTimeout

    def test_workflow_error_names_one_thing_only(self):
        """There were two unrelated WorkflowErrors; only the used one remains."""
        from dify_client.workflow import WorkflowError

        assert not hasattr(ex, "WorkflowError")
        assert WorkflowError.__module__ == "dify_client.workflow.builder"

    def test_the_exception_nobody_raised_is_gone(self):
        assert not hasattr(ex, "DatasetError")

    def test_every_exception_is_reachable_from_the_root(self):
        classes = [
            v
            for v in vars(ex).values()
            if isinstance(v, type) and issubclass(v, Exception)
        ]
        assert classes
        assert all(issubclass(c, DifyClientError) for c in classes)
