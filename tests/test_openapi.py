"""Dify's /openapi/v1 surface, against a transport that never leaves the process."""

import json

import httpx
import pytest

from dify_client import DifyManagement, OpenApiClient
from dify_client.console import CONSOLE_TOKEN_ENV, CONSOLE_URL_ENV
from dify_client.exceptions import APIError, AuthenticationError, ValidationError
from dify_client.openapi import OPENAPI_TOKEN_ENV, TOKEN_PREFIXES
from dify_client.secrets import HOST_ENV
from dify_client.workflow import Workflow, text_input

TOKEN = "dfoa_FAKETOKEN0123456789"
WS = "158046bb-cd1a-4ad2-8d52-af500dc14bc2"
APP = "f83843fb-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    for name in (OPENAPI_TOKEN_ENV, HOST_ENV, CONSOLE_TOKEN_ENV, CONSOLE_URL_ENV):
        monkeypatch.delenv(name, raising=False)


class Dify:
    """A stand-in for /openapi/v1 that records what it was asked."""

    def __init__(self, **overrides):
        self.calls: list[tuple[str, str]] = []
        self.bodies: dict[str, dict] = {}
        self.overrides = overrides

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/openapi/v1", "")
        self.calls.append((request.method, path))
        if request.content:
            try:
                self.bodies[path] = json.loads(request.content)
            except json.JSONDecodeError:
                # Multipart; keep the raw bytes and the type for the boundary.
                self.bodies[path] = {
                    "raw": request.content,
                    "content_type": request.headers.get("content-type", ""),
                }
        if path in self.overrides:
            return self.overrides[path]

        if path == "/_version":
            return httpx.Response(
                200, json={"version": "1.17.1", "edition": "COMMUNITY"}
            )
        if path == "/account":
            return httpx.Response(
                200,
                json={
                    "subject_type": "account",
                    "subject_email": "me@example.com",
                    "account": {"id": "a1", "email": "me@example.com", "name": "Me"},
                    "workspaces": [{"id": WS, "name": "W", "role": "owner"}],
                    "default_workspace_id": WS,
                },
            )
        if path == "/workspaces":
            return httpx.Response(
                200,
                json={
                    "workspaces": [
                        {"id": WS, "name": "W", "role": "owner", "current": True}
                    ]
                },
            )
        if path == "/apps":
            return httpx.Response(
                200,
                json={
                    "page": 1,
                    "limit": 20,
                    "total": 1,
                    "has_more": False,
                    "data": [{"id": APP, "name": "greeter", "mode": "workflow"}],
                },
            )
        if path.endswith("/apps/imports"):
            return httpx.Response(
                200,
                json={
                    "id": "i1",
                    "status": "completed",
                    "app_id": APP,
                    "app_mode": "workflow",
                },
            )
        if path.endswith("/dsl"):
            return httpx.Response(200, json={"data": "app:\n  mode: workflow\n"})
        return httpx.Response(200, json={})

    def client(self) -> OpenApiClient:
        http = httpx.Client(
            transport=httpx.MockTransport(self.handler),
            base_url="https://dify.test/openapi/v1",
        )
        return OpenApiClient(
            token=TOKEN, base_url="https://dify.test", http_client=http
        )


def greeter() -> Workflow:
    wf = Workflow("greeter")
    start = wf.start([text_input("name")])
    wf.connect(start, wf.end({"out": start["name"]}))
    return wf


class TestConstruction:
    def test_it_reads_the_same_token_variable_as_difyctl(self):
        """Here the name does mean the same thing — a dfoa_ bearer."""
        assert OPENAPI_TOKEN_ENV == "DIFY_TOKEN"
        assert TOKEN_PREFIXES == ("dfoa_", "dfoe_")

    def test_the_token_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(OPENAPI_TOKEN_ENV, TOKEN)
        assert OpenApiClient().token == TOKEN

    def test_no_token_anywhere_names_the_variable(self):
        with pytest.raises(ValidationError, match=OPENAPI_TOKEN_ENV):
            OpenApiClient()

    def test_the_host_is_shared_with_everything_else(self, monkeypatch):
        monkeypatch.setenv(HOST_ENV, "https://dify.internal")
        assert OpenApiClient(token=TOKEN).base_url == "https://dify.internal"

    def test_the_token_is_not_rendered(self):
        assert TOKEN not in repr(OpenApiClient(token=TOKEN))


class TestIdentity:
    def test_the_account_reply_is_returned_whole(self):
        """An external-SSO bearer has no account, so nothing is flattened away."""
        payload = Dify().client().account()
        assert payload["subject_type"] == "account"
        assert payload["account"]["email"] == "me@example.com"

    def test_the_email_is_read_out_of_the_nesting(self):
        assert Dify().client().email == "me@example.com"

    def test_workspaces_are_listed(self):
        spaces = Dify().client().workspaces()
        assert [(w.name, w.current) for w in spaces] == [("W", True)]

    def test_the_current_workspace_is_resolved_once(self):
        dify = Dify()
        client = dify.client()
        assert client.workspace_id == WS
        assert client.workspace_id == WS
        assert [p for _, p in dify.calls].count("/workspaces") == 1

    def test_a_default_workspace_counts_as_current(self):
        """/workspaces marks one; /account names a default instead."""
        dify = Dify(
            **{
                "/workspaces": httpx.Response(
                    200,
                    json={
                        "workspaces": [{"id": WS, "name": "W", "role": "owner"}],
                        "default_workspace_id": WS,
                    },
                )
            }
        )
        assert dify.client().workspace_id == WS

    def test_a_token_with_no_workspace_says_so(self):
        dify = Dify(
            **{
                "/workspaces": httpx.Response(200, json={"workspaces": []}),
                "/account": httpx.Response(
                    200, json={"subject_type": "external", "workspaces": []}
                ),
            }
        )
        with pytest.raises(ValidationError, match="no workspace"):
            _ = dify.client().workspace_id


class TestApps:
    def test_listing_scopes_to_the_workspace(self):
        dify = Dify()
        apps = dify.client().apps.list()
        assert [a.name for a in apps] == ["greeter"]

    def test_importing_goes_through_the_workspace_path(self):
        dify = Dify()
        result = dify.client().apps.deploy(greeter())
        assert result.app_id == APP
        assert ("POST", f"/workspaces/{WS}/apps/imports") in dify.calls
        body = dify.bodies[f"/workspaces/{WS}/apps/imports"]
        assert body["mode"] == "yaml-content"
        assert "kind: app" in body["yaml_content"]

    def test_overwriting_passes_the_app_id(self):
        dify = Dify()
        dify.client().apps.deploy(greeter(), app_id=APP)
        assert dify.bodies[f"/workspaces/{WS}/apps/imports"]["app_id"] == APP

    def test_export_returns_the_dsl(self):
        assert "mode: workflow" in Dify().client().apps.export(APP)

    def test_an_empty_export_is_an_error(self):
        dify = Dify(**{f"/apps/{APP}/dsl": httpx.Response(200, json={"data": ""})})
        with pytest.raises(ValidationError, match="no DSL"):
            dify.client().apps.export(APP)


class TestErrors:
    def test_an_expired_token_says_how_to_get_another(self):
        dify = Dify(
            **{"/account": httpx.Response(401, json={"message": "Unauthorized"})}
        )
        with pytest.raises(AuthenticationError, match="mint_openapi_token"):
            dify.client().account()

    def test_a_disabled_surface_is_explained(self):
        """404 with no body means the blueprint is not registered at all."""
        dify = Dify(**{"/account": httpx.Response(404, text="")})
        with pytest.raises(APIError, match="OPENAPI_ENABLED"):
            dify.client().account()

    def test_availability_needs_no_token(self):
        assert OpenApiClient.available("http://127.0.0.1:9") is False


class TestConsoleHandoff:
    def console(self, handler) -> DifyManagement:
        http = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://dify.test/console/api",
        )
        return DifyManagement(
            token="ey.console", base_url="https://dify.test", http_client=http
        )

    def test_a_session_mints_a_bearer_without_a_browser(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            calls.append(path)
            if path.endswith("/device/code"):
                return httpx.Response(
                    200, json={"user_code": "AAAA-BBBB", "device_code": "dc_1"}
                )
            if path.endswith("/device/approve"):
                return httpx.Response(200, json={"status": "approved"})
            if path.endswith("/device/token"):
                return httpx.Response(
                    200, json={"token": TOKEN, "expires_at": "2026-09-30"}
                )
            return httpx.Response(200, json={})

        assert self.console(handler).mint_openapi_token(client_id="difyctl") == TOKEN
        assert [p.rsplit("/", 1)[-1] for p in calls] == ["code", "approve", "token"]

    def test_an_unregistered_client_id_names_the_setting(self):
        def handler(request):
            return httpx.Response(400, json={"error": "unsupported_client"})

        with pytest.raises(ValidationError, match="OPENAPI_KNOWN_CLIENT_IDS"):
            self.console(handler).mint_openapi_token()

    def test_a_disabled_surface_is_reported_rather_than_retried(self):
        def handler(request):
            return httpx.Response(404, text="")

        with pytest.raises(ValidationError, match="OPENAPI_ENABLED"):
            self.console(handler).mint_openapi_token(client_id="difyctl")


class TestFileUpload:
    """POST /apps/<id>/files — the file inputs a run references by id."""

    def _dify(self):
        return Dify(
            **{
                f"/apps/{APP}/files": httpx.Response(
                    201,
                    json={
                        "id": "up-1",
                        "name": "report.pdf",
                        "size": 2,
                        "mime_type": "application/pdf",
                    },
                )
            }
        )

    def test_a_path_is_read_and_sent(self, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"hi")
        dify = self._dify()

        result = dify.client().apps.upload_file(APP, pdf)

        assert ("POST", f"/apps/{APP}/files") in dify.calls
        assert result["id"] == "up-1"

    def test_it_goes_out_as_multipart_not_json(self, tmp_path):
        """The client's usual application/json header would break the upload."""
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"hi")
        dify = self._dify()

        dify.client().apps.upload_file(APP, pdf)

        body = dify.bodies[f"/apps/{APP}/files"]
        assert body["content_type"].startswith("multipart/form-data")
        assert b"report.pdf" in body["raw"]
        assert b"application/pdf" in body["raw"]

    def test_an_open_file_works_too(self, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"hi")
        dify = self._dify()

        with pdf.open("rb") as handle:
            dify.client().apps.upload_file(APP, handle)

        assert b"report.pdf" in dify.bodies[f"/apps/{APP}/files"]["raw"]

    def test_a_directory_in_the_name_is_not_sent(self, tmp_path):
        """Only the basename belongs in the part; a path would leak the layout."""
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"hi")
        dify = self._dify()

        dify.client().apps.upload_file(APP, pdf)

        assert str(tmp_path).encode() not in dify.bodies[f"/apps/{APP}/files"]["raw"]

    def test_an_unnamed_stream_asks_for_a_filename(self):
        import io

        with pytest.raises(ValidationError, match="filename"):
            self._dify().client().apps.upload_file(APP, io.BytesIO(b"hi"))

    def test_a_filename_makes_a_stream_uploadable(self):
        import io

        dify = self._dify()
        dify.client().apps.upload_file(APP, io.BytesIO(b"hi"), filename="report.pdf")
        assert b"report.pdf" in dify.bodies[f"/apps/{APP}/files"]["raw"]

    def test_an_untypeable_name_says_so_rather_than_letting_dify_415(self, tmp_path):
        odd = tmp_path / "notes.wat"
        odd.write_bytes(b"hi")
        with pytest.raises(ValidationError, match="content_type"):
            self._dify().client().apps.upload_file(APP, odd)

    def test_an_explicit_content_type_wins(self, tmp_path):
        odd = tmp_path / "notes.wat"
        odd.write_bytes(b"hi")
        dify = self._dify()

        dify.client().apps.upload_file(APP, odd, content_type="text/plain")

        assert b"text/plain" in dify.bodies[f"/apps/{APP}/files"]["raw"]

    def test_a_rejected_upload_raises(self, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"hi")
        dify = Dify(
            **{
                f"/apps/{APP}/files": httpx.Response(
                    413, json={"message": "File too large"}
                )
            }
        )

        with pytest.raises(APIError, match="File too large"):
            dify.client().apps.upload_file(APP, pdf)
