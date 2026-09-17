"""Tests for the console client, against a transport that never leaves the process."""

import base64
import json

import httpx
import pytest

from dify_client import DifyManagement, Stage
from dify_client.console import CONSOLE_TOKEN_ENV, CONSOLE_URL_ENV
from dify_client.exceptions import APIError, AuthenticationError, ValidationError
from dify_client.workflow import Workflow, text_input

TOKEN = "ey.FAKE.CONSOLE.TOKEN.000000"


@pytest.fixture(autouse=True)
def _no_ambient_console_config(monkeypatch):
    monkeypatch.delenv(CONSOLE_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CONSOLE_URL_ENV, raising=False)


def client_for(handler, *, token: str = TOKEN) -> DifyManagement:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://dify.test/console/api")
    return DifyManagement(token=token, base_url="https://dify.test", http_client=http)


def greeter() -> Workflow:
    wf = Workflow("greeter")
    start = wf.start([text_input("name")])
    answer = wf.answer(start["name"])
    wf.connect(start, answer)
    return wf


class TestConstruction:
    def test_the_token_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(CONSOLE_TOKEN_ENV, TOKEN)
        assert DifyManagement().token == TOKEN

    def test_no_token_anywhere_names_the_variable(self):
        with pytest.raises(ValidationError, match=CONSOLE_TOKEN_ENV):
            DifyManagement()

    def test_the_error_asks_for_a_token_not_an_api_key(self):
        """These are different credentials; naming the wrong one misleads."""
        with pytest.raises(ValidationError) as caught:
            DifyManagement()
        message = str(caught.value)
        assert "console token" in message
        assert "token=" in message
        assert "api_key=" not in message

    def test_the_host_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(CONSOLE_URL_ENV, "https://dify.internal")
        assert DifyManagement(token=TOKEN).base_url == "https://dify.internal"

    def test_the_host_variable_is_shared_with_difyctl(self):
        """The host means the same thing in both; the token does not."""
        assert CONSOLE_URL_ENV == "DIFY_HOST"
        assert CONSOLE_TOKEN_ENV == "DIFY_CONSOLE_TOKEN"
        assert CONSOLE_TOKEN_ENV != "DIFY_TOKEN"

    @pytest.mark.parametrize("token", ["dfoa_abc123", "dfoe_abc123"])
    def test_an_openapi_bearer_is_refused_with_an_explanation(self, token):
        """difyctl stores one of these; the console API answers it with a bare 401."""
        with pytest.raises(ValidationError) as caught:
            DifyManagement(token=token)
        message = str(caught.value)
        assert "/openapi/v1" in message
        assert "DifyManagement.login" in message

    def test_an_ordinary_token_is_accepted(self):
        assert DifyManagement(token=TOKEN).token == TOKEN

    def test_one_host_configures_both_clients(self, monkeypatch):
        from dify_client import DifyApp

        monkeypatch.setenv(CONSOLE_URL_ENV, "https://dify.internal")
        assert DifyManagement(token=TOKEN).base_url == "https://dify.internal"
        assert (
            DifyApp(api_key="app-KEY1234567890").base_url == "https://dify.internal/v1"
        )

    def test_the_token_is_not_rendered(self):
        assert TOKEN not in repr(DifyManagement(token=TOKEN))
        assert TOKEN not in repr(vars(DifyManagement(token=TOKEN)))


def http_for(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://dify.test/console/api",
    )


class TestLogin:
    def test_it_returns_a_client_holding_the_access_token(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/login")
            return httpx.Response(
                200, json={"result": "success", "data": {"access_token": TOKEN}}
            )

        console = DifyManagement.login(
            "a@example.com",
            "hunter2",
            base_url="https://dify.test",
            http_client=http_for(handler),
        )
        assert console.token == TOKEN

    def test_the_password_is_base64_encoded_as_the_server_expects(self):
        """Dify base64-decodes this field; a plaintext password is rejected."""
        sent = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content))
            return httpx.Response(
                200, json={"result": "success", "data": {"access_token": TOKEN}}
            )

        DifyManagement.login(
            "a@example.com",
            "hunter2",
            base_url="https://dify.test",
            http_client=http_for(handler),
        )
        assert sent["password"] != "hunter2"
        assert base64.b64decode(sent["password"]).decode() == "hunter2"

    def test_the_password_is_not_kept_on_the_client(self):
        def handler(request):
            return httpx.Response(
                200, json={"result": "success", "data": {"access_token": TOKEN}}
            )

        console = DifyManagement.login(
            "a@example.com",
            "hunter2",
            base_url="https://dify.test",
            http_client=http_for(handler),
        )
        assert "hunter2" not in repr(vars(console))

    def test_a_workspaceless_account_is_an_authentication_error(self):
        def handler(request):
            return httpx.Response(
                200, json={"result": "fail", "data": "workspace not found"}
            )

        with pytest.raises(AuthenticationError, match="workspace not found"):
            DifyManagement.login(
                "a@example.com",
                "hunter2",
                base_url="https://dify.test",
                http_client=http_for(handler),
            )

    def test_bad_credentials_are_an_authentication_error(self):
        def handler(request):
            return httpx.Response(401, json={"message": "invalid credentials"})

        with pytest.raises(AuthenticationError):
            DifyManagement.login(
                "a@example.com",
                "wrong",
                base_url="https://dify.test",
                http_client=http_for(handler),
            )

    def test_a_response_without_a_token_is_an_error(self):
        def handler(request):
            return httpx.Response(200, json={"result": "success", "data": {}})

        with pytest.raises(AuthenticationError, match="no access token"):
            DifyManagement.login(
                "a@example.com",
                "hunter2",
                base_url="https://dify.test",
                http_client=http_for(handler),
            )


class TestImport:
    def test_a_workflow_is_sent_as_yaml_content(self):
        sent = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/apps/imports")
            sent.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "i1",
                    "status": "completed",
                    "app_id": "a1",
                    "app_mode": "advanced-chat",
                },
            )

        result = client_for(handler).apps.deploy(greeter())
        assert result.app_id == "a1"
        assert sent["mode"] == "yaml-content"
        assert "kind: app" in sent["yaml_content"]
        assert sent["name"] == "greeter"

    def test_the_token_signs_the_request(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(
                200, json={"id": "i1", "status": "completed", "app_id": "a1"}
            )

        client_for(handler).apps.import_definition("kind: app")
        assert seen["auth"] == f"Bearer {TOKEN}"

    def test_passing_an_app_id_overwrites_that_app(self):
        sent = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content))
            return httpx.Response(
                200, json={"id": "i1", "status": "completed", "app_id": "a1"}
            )

        client_for(handler).apps.deploy(greeter(), app_id="a1")
        assert sent["app_id"] == "a1"

    def test_completed_with_warnings_still_counts_as_deployed(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "id": "i1",
                    "status": "completed-with-warnings",
                    "app_id": "a1",
                    "warnings": [
                        {"code": "x", "path": "/", "message": "a tool was not found"}
                    ],
                },
            )

        result = client_for(handler).apps.deploy(greeter())
        assert result.imported
        assert result.warnings[0]["message"] == "a tool was not found"

    def test_a_failed_import_reports_the_server_reason(self):
        """Deploy reports a stage rather than raising: "nothing happened" and
        "the app exists but is unpublished" are different situations."""

        def handler(request):
            return httpx.Response(
                400, json={"id": "i1", "status": "failed", "error": "Missing app data"}
            )

        result = client_for(handler).apps.deploy(greeter())
        assert result.stage is Stage.NOT_IMPORTED
        assert "Missing app data" in result.error

    def test_a_failed_import_raises_when_asked_to(self):
        def handler(request):
            return httpx.Response(
                400, json={"id": "i1", "status": "failed", "error": "Missing app data"}
            )

        with pytest.raises(APIError, match="Nothing was created"):
            client_for(handler).apps.deploy(greeter()).raise_for_stage()

    def test_a_version_gap_asks_for_confirmation_rather_than_failing_silently(self):
        def handler(request):
            return httpx.Response(
                202,
                json={
                    "id": "i1",
                    "status": "pending",
                    "imported_dsl_version": "0.7.0",
                    "current_dsl_version": "0.9.0",
                },
            )

        result = client_for(handler).apps.deploy(greeter())
        assert result.stage is Stage.NOT_IMPORTED
        assert "confirmed" in result.error

    def test_a_pending_import_can_be_confirmed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/apps/imports/i1/confirm")
            return httpx.Response(
                200, json={"id": "i1", "status": "completed", "app_id": "a1"}
            )

        assert client_for(handler)._confirm_import("i1").app_id == "a1"

    def test_an_expired_token_says_so(self):
        def handler(request):
            return httpx.Response(401, json={"message": "Unauthorized"})

        result = client_for(handler).apps.import_definition("kind: app")
        assert result.stage is Stage.NOT_IMPORTED
        assert "short-lived" in result.error

    def test_an_unexpected_server_error_surfaces(self):
        def handler(request):
            return httpx.Response(500, json={"message": "boom"})

        result = client_for(handler).apps.import_definition("kind: app")
        assert result.stage is Stage.NOT_IMPORTED
        assert "boom" in result.error


class TestApiKeys:
    def test_creating_a_key_returns_it_in_full(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            return httpx.Response(
                200, json={"id": "k1", "token": "app-REALKEY1234567890", "type": "app"}
            )

        key = client_for(handler).apps.keys.create("a1")
        assert key.token == "app-REALKEY1234567890"
        assert not key.is_masked

    def test_listed_keys_are_masked_and_say_so(self):
        """Dify reveals a key once; a listing cannot be used to authenticate."""

        def handler(request):
            return httpx.Response(
                200,
                json={"data": [{"id": "k1", "token": "app-****7890", "type": "app"}]},
            )

        keys = client_for(handler).apps.keys.list("a1")
        assert keys[0].is_masked


class TestSecretsInTheDeployedDocument:
    def test_secret_environment_variables_are_not_deployed(self):
        """Their values live in Dify; a deploy must not blank or leak them."""
        sent = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content))
            return httpx.Response(
                200, json={"id": "i1", "status": "completed", "app_id": "a1"}
            )

        wf = greeter()
        wf.env_var("API_TOKEN", "t0p-s3cret-value", secret=True)
        client_for(handler).apps.deploy(wf)
        assert "t0p-s3cret-value" not in sent["yaml_content"]
        assert "API_TOKEN" in sent["yaml_content"]


class TestCsrfToken:
    """Dify 1.17 pairs the access token with a second one for writes."""

    def test_it_is_read_from_the_environment(self, monkeypatch):
        from dify_client.console import CONSOLE_CSRF_ENV

        monkeypatch.setenv(CONSOLE_CSRF_ENV, "csrf-abc")
        assert DifyManagement(token=TOKEN)._csrf_token == "csrf-abc"

    def test_an_argument_wins_over_the_environment(self, monkeypatch):
        from dify_client.console import CONSOLE_CSRF_ENV

        monkeypatch.setenv(CONSOLE_CSRF_ENV, "from-env")
        assert (
            DifyManagement(token=TOKEN, csrf_token="explicit")._csrf_token == "explicit"
        )

    def test_without_one_neither_the_header_nor_the_cookie_is_sent(self):
        from dify_client.console import CSRF_HEADER

        headers = DifyManagement(token=TOKEN)._headers()
        assert CSRF_HEADER not in headers
        assert "csrf_token" not in headers["Cookie"]

    def test_with_one_it_goes_out_as_both(self):
        """Dify compares the header against the cookie and rejects a mismatch."""
        from dify_client.console import CSRF_HEADER

        headers = DifyManagement(token=TOKEN, csrf_token="csrf-abc")._headers()
        assert headers[CSRF_HEADER] == "csrf-abc"
        assert "csrf_token=csrf-abc" in headers["Cookie"]

    def test_a_csrf_401_says_what_is_actually_missing(self):
        """ "Get a fresh token" would send the reader the wrong way entirely."""

        def handler(request):
            return httpx.Response(
                401, json={"message": "CSRF token is missing or invalid."}
            )

        with pytest.raises(AuthenticationError) as caught:
            client_for(handler).apps.triggers.list("app-1")

        message = str(caught.value)
        assert "CSRF" in message
        assert "read but not write" in message
        assert "DIFY_CONSOLE_CSRF_TOKEN" in message

    def test_an_ordinary_401_still_reads_as_an_expired_token(self):
        def handler(request):
            return httpx.Response(401, json={"message": "Unauthorized"})

        with pytest.raises(AuthenticationError, match="short-lived"):
            client_for(handler).apps.triggers.list("app-1")
