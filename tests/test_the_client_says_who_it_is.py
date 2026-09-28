"""What the client tells Dify about itself, and what it keeps from the answer.

Three things were missing in both directions. Dify saw `python-httpx/0.28.1`
and could not tell an SDK call from a curl script. The SDK threw away
`X-Version` and `X-Env`, which are on every response including the errors, so a
bug report never said which Dify it was. And a 429 raised on sight, leaving
every caller to write the same sleep loop.
"""

import asyncio
from importlib.metadata import version as installed_version

import httpx
import pytest

import dify_client
from dify_client import DifyApp
from dify_client.base_client import (
    MAX_RATE_LIMIT_WAIT,
    rate_limit_wait,
    retry_after_seconds,
)
from dify_client.exceptions import APIError, AuthenticationError, RateLimitError
from dify_client.version import USER_AGENT, __version__, user_agent

DIFY_HEADERS = {"X-Version": "1.17.1", "X-Env": "PRODUCTION"}


class Recorder:
    """Answers with whatever is queued, and keeps every request."""

    def __init__(self, *responses):
        self.queued = list(responses)
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        reply = self.queued.pop(0) if len(self.queued) > 1 else self.queued[0]
        return reply() if callable(reply) else reply

    def app(self, *, timeout=5.0, **kwargs):
        kwargs.setdefault("retry_delay", 0)
        # The timeout goes on the HTTP client, because that is where it lives
        # when a caller brings their own — `DifyApp(timeout=…)` configures the
        # client this SDK would have built instead.
        return DifyApp(
            "app-key",
            user="alice",
            http_client=httpx.Client(
                transport=httpx.MockTransport(self.handler),
                base_url="https://dify.test/v1",
                timeout=timeout,
            ),
            **kwargs,
        )


def ok(payload=None, status=200, headers=None):
    return httpx.Response(
        status, json=payload or {"name": "app", "mode": "workflow"}, headers=headers
    )


class TestItHasAVersion:
    def test_the_package_reports_one(self):
        assert dify_client.__version__ == __version__

    def test_it_is_the_installed_one(self):
        """Read from the distribution rather than written down twice."""
        assert __version__ == installed_version("dify-client")

    def test_the_user_agent_names_the_sdk_and_the_runtime(self):
        assert USER_AGENT.startswith(f"dify-client/{__version__}")
        assert "python/" in USER_AGENT
        assert "httpx/" in USER_AGENT


class TestItIntroducesItself:
    def test_every_request_carries_the_user_agent(self):
        recorder = Recorder(ok())
        with recorder.app() as app:
            app.info()

        assert recorder.requests[0].headers["user-agent"] == USER_AGENT

    def test_an_upload_carries_it_too(self, tmp_path):
        note = tmp_path / "note.txt"
        note.write_text("x")
        recorder = Recorder(ok({"id": "f-1", "name": "note.txt"}))
        with recorder.app() as app:
            app.files.upload(note)

        sent = recorder.requests[0]
        assert sent.headers["user-agent"] == USER_AGENT

    def test_an_upload_still_lets_httpx_set_the_boundary(self, tmp_path):
        """Sending our own `Content-Type` would break the multipart body."""
        note = tmp_path / "note.txt"
        note.write_text("x")
        recorder = Recorder(ok({"id": "f-1", "name": "note.txt"}))
        with recorder.app() as app:
            app.files.upload(note)

        assert "boundary=" in recorder.requests[0].headers["content-type"]

    def test_a_stream_carries_it(self):
        recorder = Recorder(httpx.Response(200, text="data: {}\n\n"))
        with recorder.app() as app:
            with app.workflows.runs.stream({"x": 1}) as stream:
                list(stream)

        assert recorder.requests[0].headers["user-agent"] == USER_AGENT

    def test_httpxs_own_default_is_replaced(self):
        assert user_agent("python-httpx/0.28.1") == USER_AGENT

    def test_a_callers_agent_is_kept_and_ours_appended(self):
        """A caller who set one said something about who they are."""
        assert user_agent("acme-bot/2.0") == f"acme-bot/2.0 {USER_AGENT}"

    def test_it_is_not_appended_twice(self):
        once = user_agent("acme-bot/2.0")
        assert user_agent(once) == once


class TestAnErrorSaysWhichDify:
    def _fails(self, status=401, headers=DIFY_HEADERS):
        recorder = Recorder(
            httpx.Response(status, json={"message": "bad key"}, headers=headers)
        )
        with recorder.app(max_retries=0) as app:
            with pytest.raises(APIError) as caught:
                app.info()
        return caught.value

    def test_the_version_is_on_the_exception(self):
        assert self._fails().server_version == "1.17.1"

    def test_the_environment_is_too(self):
        assert self._fails().server_env == "PRODUCTION"

    def test_the_message_carries_it_where_a_traceback_will_show_it(self):
        assert str(self._fails()) == "bad key [Dify 1.17.1]"

    def test_the_message_alone_is_still_available(self):
        assert self._fails().message == "bad key"

    def test_a_server_that_says_nothing_adds_nothing(self):
        failure = self._fails(headers={})
        assert str(failure) == "bad key"
        assert failure.server_version == ""

    def test_the_type_is_still_the_one_that_names_the_failure(self):
        assert isinstance(self._fails(401), AuthenticationError)

    def test_a_trace_id_comes_through_when_dify_is_tracing(self):
        failure = self._fails(headers={**DIFY_HEADERS, "X-Trace-Id": "t-1"})
        assert failure.trace_id == "t-1"


class TestARateLimitIsWaitedOut:
    def test_a_429_is_repeated_rather_than_raised(self):
        recorder = Recorder(httpx.Response(429, json={"message": "slow"}), ok())
        with recorder.app() as app:
            assert app.info().mode == "workflow"
        assert len(recorder.requests) == 2

    def test_a_post_is_repeated_too(self):
        """Unlike a timeout: a 429 means Dify refused the request, so there is
        nothing it might already have done and billed for."""
        recorder = Recorder(
            httpx.Response(429, json={"message": "slow"}),
            ok({"task_id": "t", "data": {"status": "succeeded", "outputs": {}}}),
        )
        with recorder.app() as app:
            assert app.workflows.runs.create({"x": 1}).succeeded
        assert [r.method for r in recorder.requests] == ["POST", "POST"]

    def test_it_gives_up_and_raises_when_the_attempts_run_out(self):
        recorder = Recorder(httpx.Response(429, json={"message": "slow"}))
        with recorder.app(max_retries=2) as app:
            with pytest.raises(RateLimitError):
                app.info()
        assert len(recorder.requests) == 3

    def test_none_of_that_happens_when_retries_are_off(self):
        recorder = Recorder(httpx.Response(429, json={"message": "slow"}))
        with recorder.app(max_retries=0) as app:
            with pytest.raises(RateLimitError):
                app.info()
        assert len(recorder.requests) == 1

    def test_the_async_client_waits_the_same_way(self):
        recorder = Recorder(httpx.Response(429, json={"message": "slow"}), ok())

        async def call():
            from dify_client import AsyncDifyApp

            async with AsyncDifyApp(
                "app-key",
                user="alice",
                retry_delay=0,
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(recorder.handler),
                    base_url="https://dify.test/v1",
                ),
            ) as app:
                return await app.info()

        assert asyncio.run(call()).mode == "workflow"
        assert len(recorder.requests) == 2


class TestHowLongToWait:
    """The arithmetic, without any actual waiting."""

    def _reply(self, status=429, retry_after=None):
        headers = {"Retry-After": retry_after} if retry_after else {}
        return httpx.Response(status, headers=headers)

    def test_the_servers_hint_wins_over_the_backoff(self):
        wait = rate_limit_wait(self._reply(retry_after="5"), 0, 3, 1.0)
        assert wait == 5

    def test_without_a_hint_it_backs_off(self):
        assert rate_limit_wait(self._reply(), 0, 3, 1.0) == 1.0
        assert rate_limit_wait(self._reply(), 1, 3, 1.0) == 2.0

    def test_an_unreasonable_hint_is_refused_rather_than_slept_through(self):
        """A server under maintenance answers `Retry-After: 3600`. Blocking for
        an hour is indistinguishable from hanging."""
        assert rate_limit_wait(self._reply(retry_after="3600"), 0, 3, 1.0) is None
        assert MAX_RATE_LIMIT_WAIT < 3600

    def test_the_last_attempt_does_not_wait(self):
        assert rate_limit_wait(self._reply(retry_after="1"), 3, 3, 1.0) is None

    def test_anything_but_a_429_is_left_alone(self):
        assert rate_limit_wait(self._reply(status=500), 0, 3, 1.0) is None

    def test_a_delay_in_seconds_is_read(self):
        assert retry_after_seconds("30") == 30

    def test_nonsense_is_not_guessed_at(self):
        assert retry_after_seconds("soon") is None
        assert retry_after_seconds(None) is None

    def test_a_date_in_the_past_is_no_wait_at_all(self):
        assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0


class TestOneCallCanHaveItsOwnTimeout:
    """One number cannot serve both a conversation read and a workflow that
    runs for twenty minutes."""

    def _timeout_of(self, request):
        return request.extensions.get("timeout", {})

    def test_the_block_sets_it(self):
        recorder = Recorder(ok())
        with recorder.app() as app:
            with app.with_timeout(600):
                app.info()

        assert self._timeout_of(recorder.requests[0])["read"] == 600

    def test_outside_the_block_the_clients_own_timeout_is_used(self):
        recorder = Recorder(ok())
        with recorder.app(timeout=12.0) as app:
            with app.with_timeout(600):
                app.info()
            app.info()

        assert self._timeout_of(recorder.requests[1])["read"] == 12.0

    def test_it_is_restored_even_when_the_call_fails(self):
        recorder = Recorder(httpx.Response(500, json={"message": "boom"}))
        with recorder.app(max_retries=0, timeout=12.0) as app:
            with pytest.raises(APIError):
                with app.with_timeout(600):
                    app.info()
            with pytest.raises(APIError):
                app.info()

        assert self._timeout_of(recorder.requests[1])["read"] == 12.0

    def test_a_stream_gets_it_too(self):
        recorder = Recorder(httpx.Response(200, text="data: {}\n\n"))
        with recorder.app() as app:
            with app.with_timeout(600), app.workflows.runs.stream({"x": 1}) as stream:
                list(stream)

        assert self._timeout_of(recorder.requests[0])["read"] == 600

    def test_an_upload_gets_it_too(self, tmp_path):
        note = tmp_path / "note.txt"
        note.write_text("x")
        recorder = Recorder(ok({"id": "f-1", "name": "note.txt"}))
        with recorder.app() as app:
            with app.with_timeout(600):
                app.files.upload(note)

        assert self._timeout_of(recorder.requests[0])["read"] == 600

    def test_two_tasks_do_not_share_a_deadline(self):
        """Scoped to the task, not the client: raising it for one long call
        must not raise it for everything else in flight."""
        from dify_client import AsyncDifyApp

        recorder = Recorder(ok())

        async def call(app, seconds):
            if seconds is None:
                await app.info()
                return
            with app.with_timeout(seconds):
                await app.info()

        async def both():
            async with AsyncDifyApp(
                "app-key",
                user="alice",
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(recorder.handler),
                    base_url="https://dify.test/v1",
                    timeout=12.0,
                ),
            ) as app:
                await asyncio.gather(call(app, 600), call(app, None))

        asyncio.run(both())
        reads = {self._timeout_of(r)["read"] for r in recorder.requests}
        assert reads == {600, 12.0}
