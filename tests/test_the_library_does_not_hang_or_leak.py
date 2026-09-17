"""Failures a caller cannot see coming: a loop that never ends, a secret in a
place nobody looks at, a price that quietly reads as zero.

Found by reviewing the SDK against a server that misbehaves rather than one
that answers the way the happy path expects. None of them raise; that is what
makes them worth a test.
"""

import asyncio
import itertools
import json
import logging

import httpx
import pytest

from dify_client import AsyncDifyApp, DifyApp
from dify_client.streams import collect_run
from dify_client.usage import Usage


def _always(payload):
    """A server that answers every request the same way, counting the asks."""
    asked = itertools.count()

    def handler(request):
        if next(asked) > 20:
            raise AssertionError("this listing does not terminate")
        return httpx.Response(200, json=payload)

    return handler, asked


class TestAListingAlwaysTerminates:
    """`has_more: true` with a cursor that does not move is a page that
    fetches itself forever. Dify does it when the cursor message has been
    deleted, and a caching proxy can do it for any listing."""

    SAME_PAGE = {
        "data": [{"id": "m1", "query": "q", "answer": "a"}],
        "has_more": True,
        "limit": 1,
    }

    def _app(self, handler):
        return DifyApp(
            "app-key",
            user="alice",
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url="https://dify.test/v1",
            ),
        )

    def test_a_cursor_that_does_not_advance_stops_the_walk(self):
        handler, asked = _always(self.SAME_PAGE)
        with self._app(handler) as app:
            items = list(app.chat.messages.list("c-1", limit=1).all())

        assert items, "it should still yield what it was given"
        assert next(asked) <= 3, "it kept asking for the same page"

    def test_the_page_says_it_cannot_be_continued(self):
        handler, _ = _always(self.SAME_PAGE)
        with self._app(handler) as app:
            page = app.chat.messages.list("c-1", limit=1)
            second = page.next_page()

        assert second is not None
        assert second.next_page() is None

    def test_the_async_walk_stops_too(self):
        handler, asked = _always(self.SAME_PAGE)

        async def call():
            async with AsyncDifyApp(
                "app-key",
                user="alice",
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(handler),
                    base_url="https://dify.test/v1",
                ),
            ) as app:
                page = await app.chat.messages.list("c-1", limit=1)
                return [message async for message in page.all()]

        assert asyncio.run(call())
        assert next(asked) <= 3

    def test_a_listing_that_does_advance_is_unaffected(self):
        pages = [
            {"data": [{"id": "m2"}, {"id": "m3"}], "has_more": True, "limit": 2},
            {"data": [{"id": "m1"}], "has_more": False, "limit": 2},
        ]

        def handler(request):
            return httpx.Response(
                200, json=pages.pop(0) if len(pages) > 1 else pages[0]
            )

        with self._app(handler) as app:
            assert [m.id for m in app.chat.messages.list("c-1", limit=2).all()] == [
                "m2",
                "m3",
                "m1",
            ]

    def test_an_empty_page_claiming_more_still_stops(self):
        handler, asked = _always({"data": [], "has_more": True, "limit": 1})
        with self._app(handler) as app:
            assert list(app.chat.messages.list("c-1", limit=1).all()) == []
        assert next(asked) == 1


class TestSecretsStayOutOfPlacesNobodyReads:
    def test_a_form_token_is_not_listed_as_a_node(self):
        """`paused_nodes` is for node ids. A form Dify raised without naming a
        node was filed under its token, and the token showed up there."""
        raised = {
            "event": "human_input_required",
            "task_id": "t",
            "data": {"form_token": "tok-secret", "expiration_time": 1},
        }
        run = collect_run([f"data: {json.dumps(raised)}\n"])

        assert run.paused
        assert run.paused_nodes == []
        assert run.pending_forms == ["tok-secret"]

    def test_an_upload_is_logged_by_name_and_size_not_by_content(self, caplog):
        """A debug log is not the place for the contents of somebody's file.

        The client caps its own logger at INFO, so these lines only reach a
        handler when the application asks for DEBUG — which is exactly when
        somebody is reading them.
        """
        import io

        secret = b"PROPRIETARY-CONTENTS-" + b"x" * 200

        def handler(request):
            return httpx.Response(200, json={"id": "f-1", "name": "note.txt"})

        app = DifyApp(
            "app-key",
            user="alice",
            enable_logging=True,
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url="https://dify.test/v1",
            ),
        )
        app.logger.setLevel(logging.DEBUG)
        with caplog.at_level(logging.DEBUG, logger=app.logger.name), app:
            app.files.upload(io.BytesIO(secret), filename="note.txt")

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "PROPRIETARY" not in logged
        assert "note.txt" in logged
        assert "bytes" in logged

    def test_a_key_is_masked_in_every_repr(self):
        app = DifyApp("app-1234567890abcdef", user="alice")
        try:
            assert "1234567890" not in repr(app)
            assert "app-****cdef" in repr(app)
            assert "1234567890" not in repr(app.chat.messages)
        finally:
            app.close()


class TestAPriceIsNotErasedByAZero:
    """`workflow_finished` carries no price at all; `message_end` carries one
    that can be zero for a model Dify has no pricing for. Taking the aggregate
    over per-node figures that were actually charged reports a billed run as
    free — which is what `costs=None` exists to prevent."""

    def test_an_observed_price_survives_a_reported_zero(self):
        from decimal import Decimal

        observed = Usage(total_tokens=30, costs={"USD": Decimal("0.002")})
        reported_free = Usage(total_tokens=30, costs={})

        assert observed.merged_with(reported_free).total_price == Decimal("0.002")

    def test_a_reported_price_still_wins_over_an_observed_one(self):
        from decimal import Decimal

        observed = Usage(total_tokens=10, costs={"USD": Decimal("0.001")})
        reported = Usage(total_tokens=30, costs={"USD": Decimal("0.005")})

        assert observed.merged_with(reported).total_price == Decimal("0.005")

    def test_free_is_still_reported_when_that_is_all_anyone_said(self):
        merged = Usage(total_tokens=30).merged_with(Usage(total_tokens=30, costs={}))

        assert merged.cost_known
        assert merged.total_price == 0

    def test_unknown_stays_unknown(self):
        merged = Usage(total_tokens=30).merged_with(Usage(total_tokens=30))

        assert not merged.cost_known
        assert merged.total_price is None

    def test_a_streamed_run_keeps_its_node_prices(self):
        """The whole case, end to end: nodes report a price, the run total
        reports tokens and nothing else."""
        events = [
            {
                "event": "node_finished",
                "task_id": "t",
                "data": {
                    "node_id": "llm",
                    "id": "exec-1",
                    "status": "succeeded",
                    "execution_metadata": {
                        "total_tokens": 30,
                        "total_price": "0.002",
                        "currency": "USD",
                    },
                },
            },
            {
                "event": "workflow_finished",
                "task_id": "t",
                "data": {"status": "succeeded", "outputs": {}, "total_tokens": 30},
            },
        ]
        run = collect_run(f"data: {json.dumps(event)}\n" for event in events)

        assert run.usage.total_tokens == 30
        assert run.usage.cost_known
        assert run.usage.total_price > 0


@pytest.mark.parametrize("client", [DifyApp, AsyncDifyApp])
def test_a_client_never_prints_its_key(client):
    made = client("app-1234567890abcdef", user="alice")
    assert "1234567890" not in repr(made)


class TestAWalkHasACeiling:
    """A page-numbered listing advances its own number, so a server that never
    stops saying `has_more` is not caught by the cursor check — it is just an
    unbounded number of requests inside what reads like a `for` loop."""

    FULL_PAGE = {
        "data": [{"id": f"r{i}"} for i in range(100)],
        "has_more": True,
        "limit": 100,
    }

    def _app(self):
        handler, self.asked = _always(self.FULL_PAGE)

        def unlimited(request):
            return httpx.Response(200, json=self.FULL_PAGE)

        return DifyApp(
            "app-key",
            user="alice",
            http_client=httpx.Client(
                transport=httpx.MockTransport(unlimited),
                base_url="https://dify.test/v1",
            ),
        )

    def test_it_stops_and_says_so_rather_than_running_forever(self):
        from dify_client import MAX_WALK, PageLimitReached

        with self._app() as app:
            with pytest.raises(PageLimitReached) as reached:
                list(app.workflows.runs.logs().all())

        assert reached.value.limit == MAX_WALK
        assert "still says there are more" in str(reached.value)

    def test_what_was_read_comes_back_with_the_failure(self):
        """So a caller who hits the ceiling is not made to walk it again."""
        from dify_client import PageLimitReached

        with self._app() as app:
            with pytest.raises(PageLimitReached) as reached:
                list(app.workflows.runs.logs().all(max_items=250))

        assert len(reached.value.items) == 250

    def test_the_ceiling_can_be_raised_for_a_listing_that_is_really_that_long(self):
        pages = [
            {"data": [{"id": "a"}], "has_more": True, "limit": 1},
            {"data": [{"id": "b"}], "has_more": False, "limit": 1},
        ]

        def handler(request):
            return httpx.Response(
                200, json=pages.pop(0) if len(pages) > 1 else pages[0]
            )

        app = DifyApp(
            "app-key",
            user="alice",
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url="https://dify.test/v1",
            ),
        )
        with app:
            assert len(list(app.workflows.runs.logs().all(max_items=2))) == 2

    def test_a_listing_that_ends_never_sees_the_ceiling(self):
        page = {"data": [{"id": "a"}], "has_more": False, "limit": 1}

        def handler(request):
            return httpx.Response(200, json=page)

        app = DifyApp(
            "app-key",
            user="alice",
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url="https://dify.test/v1",
            ),
        )
        with app:
            assert len(list(app.workflows.runs.logs().all(max_items=1))) == 1

    def test_the_async_walk_has_the_same_ceiling(self):
        from dify_client import PageLimitReached

        def handler(request):
            return httpx.Response(200, json=self.FULL_PAGE)

        async def call():
            async with AsyncDifyApp(
                "app-key",
                user="alice",
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(handler),
                    base_url="https://dify.test/v1",
                ),
            ) as app:
                page = await app.workflows.runs.logs()
                return [item async for item in page.all(max_items=150)]

        with pytest.raises(PageLimitReached):
            asyncio.run(call())


class TestATimeoutThatWouldBeIgnoredIsRefused:
    """An httpx client carries its own timeout, and a request made through one
    uses that. `timeout=` beside `http_client=` was accepted, stored, and then
    had no effect — so the caller who passed 5 seconds did not get 5 seconds
    and was told nothing."""

    def _client(self):
        return httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
            base_url="https://dify.test/v1",
        )

    @pytest.mark.parametrize("client", [DifyApp, AsyncDifyApp])
    def test_passing_both_is_refused(self, client):
        from dify_client.exceptions import ValidationError

        with pytest.raises(ValidationError, match="would be ignored"):
            client("app-key", timeout=5.0, http_client=self._client())

    def test_the_message_says_where_to_put_it_instead(self):
        from dify_client.exceptions import ValidationError

        with pytest.raises(ValidationError, match="httpx.Client"):
            DifyApp("app-key", timeout=5.0, http_client=self._client())

    def test_the_management_client_refuses_it_too(self):
        from dify_client import DifyManagement
        from dify_client.exceptions import ValidationError

        with pytest.raises(ValidationError, match="would be ignored"):
            DifyManagement("token", timeout=5.0, http_client=self._client())

    def test_a_timeout_on_its_own_still_reaches_the_connection(self):
        app = DifyApp("app-key", timeout=7.0)
        try:
            assert app.timeout == 7.0
            assert app._client.timeout.read == 7.0
        finally:
            app.close()

    def test_the_default_is_used_when_nothing_is_said(self):
        from dify_client.base_client import DEFAULT_TIMEOUT

        app = DifyApp("app-key")
        try:
            assert app.timeout == DEFAULT_TIMEOUT
        finally:
            app.close()

    def test_bringing_a_client_without_a_timeout_argument_is_fine(self):
        app = DifyApp("app-key", http_client=self._client())
        try:
            assert app.timeout  # the default, unused but reported
        finally:
            app.close()


class TestLoggingDoesNotSwallowItsOwnDetail:
    def test_a_debug_handler_receives_the_debug_lines(self, caplog):
        """`enable_logging=True` used to pin the logger at INFO, which deleted
        every debug line for anyone who went looking for them."""

        def handler(request):
            return httpx.Response(200, json={"name": "app", "mode": "workflow"})

        app = DifyApp(
            "app-key",
            user="alice",
            enable_logging=True,
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url="https://dify.test/v1",
            ),
        )
        with caplog.at_level(logging.DEBUG, logger=app.logger.name), app:
            app.chat.messages.create("hello")

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "Request body" in logged, "the debug lines never reached a handler"

    def test_the_logger_is_not_left_capped(self):
        app = DifyApp("app-key", enable_logging=True)
        try:
            assert app.logger.level == logging.DEBUG
        finally:
            app.close()


class TestAPriceWithoutACurrencyIsStillAPrice:
    def test_the_amount_is_kept(self):
        from decimal import Decimal

        usage = Usage.from_metadata({"total_price": "0.002", "total_tokens": 30})

        assert usage.cost_known
        assert usage.total_price == Decimal("0.002")

    def test_the_unit_is_reported_as_unknown_rather_than_invented(self):
        usage = Usage.from_metadata({"total_price": "0.002"})

        assert usage.currency == ""

    def test_it_reads_without_a_dangling_unit(self):
        usage = Usage.from_metadata({"total_price": "0.002", "total_tokens": 30})

        assert str(usage).endswith("· 0.002")

    def test_a_currency_is_still_used_when_dify_sends_one(self):
        usage = Usage.from_metadata(
            {"total_price": "0.002", "currency": "USD", "total_tokens": 30}
        )

        assert usage.currency == "USD"
        assert str(usage).endswith("· 0.002 USD")

    def test_no_price_at_all_is_still_unknown(self):
        assert not Usage.from_metadata({"total_tokens": 30}).cost_known
