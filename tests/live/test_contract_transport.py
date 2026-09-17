"""What crosses the wire, against a real Dify.

Headers are the kind of thing a mock will happily agree with: the SDK sent
`python-httpx/0.28.1` for years and every test passed, and `X-Version` — which
Dify puts on every single response — was never read.
"""

import httpx
import pytest

from dify_client import DifyApp
from dify_client.exceptions import AuthenticationError, RequestTimeout
from dify_client.version import USER_AGENT, __version__


class TestItSaysWhoItIs:
    def test_the_user_agent_that_reaches_dify_names_this_sdk(
        self, workflow_deployment, service_api
    ):
        """Read off the request as it leaves, on a call Dify really answered.
        A header assembled correctly and then overwritten by the HTTP client
        looks the same from the inside."""
        sent = []
        client = httpx.Client(
            base_url=service_api, event_hooks={"request": [sent.append]}
        )
        with DifyApp(
            workflow_deployment.api_key,
            http_client=client,
            user="sdk-harness",
        ) as app:
            assert app.info().mode == "workflow"

        assert sent[0].headers["user-agent"] == USER_AGENT
        assert USER_AGENT.startswith(f"dify-client/{__version__}")


class TestAFailureSaysWhichDify:
    def test_the_running_version_is_on_the_exception(self, service_api, host):
        running = DifyApp.probe(f"{host}/v1").server_version

        with DifyApp("app-INVALID", base_url=service_api, user="sdk-harness") as app:
            with pytest.raises(AuthenticationError) as caught:
                app.info()

        assert caught.value.server_version == running
        assert caught.value.server_env
        assert running in str(caught.value)


class TestOneCallCanHaveItsOwnTimeout:
    def test_a_short_one_gives_up(self, workflow_deployment, service_api):
        """A deadline that cannot be met has to end the call, not be ignored."""
        with DifyApp(
            workflow_deployment.api_key,
            base_url=service_api,
            user="sdk-harness",
            max_retries=0,
        ) as app:
            with pytest.raises(RequestTimeout), app.with_timeout(0.001):
                app.info()

    def test_the_client_goes_back_to_its_own_afterwards(
        self, workflow_deployment, service_api
    ):
        with DifyApp(
            workflow_deployment.api_key,
            base_url=service_api,
            user="sdk-harness",
            max_retries=0,
        ) as app:
            with pytest.raises(RequestTimeout), app.with_timeout(0.001):
                app.info()

            assert app.info().mode == "workflow"
