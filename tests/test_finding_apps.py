"""Getting hold of an app you already have, and picking the right client.

Both were missing. Listing apps meant reaching into `console._client` — every
verification script written against this SDK did exactly that — and choosing
between ChatClient, CompletionClient and WorkflowClient meant knowing the app's
mode already, with a wrong guess answering "check if your app mode matches the
right API route" and nothing more.
"""

import httpx
import pytest

from dify_client import App, DifyApp, DifyManagement
from dify_client.exceptions import ValidationError

APPS = [
    {"id": "a1", "name": "triage", "mode": "advanced-chat", "description": "d1"},
    {"id": "a2", "name": "notes", "mode": "workflow", "description": ""},
    {"id": "a3", "name": "notes", "mode": "completion", "description": ""},
]


class Dify:
    def __init__(self, apps=APPS):
        self.apps = apps
        self.params = {}

    def handler(self, request):
        path = request.url.path.replace("/console/api", "")
        self.params = dict(request.url.params)
        if path == "/apps":
            data = self.apps
            if mode := self.params.get("mode"):
                data = [a for a in data if a["mode"] == mode]
            if name := self.params.get("name"):
                data = [a for a in data if name in a["name"]]
            return httpx.Response(200, json={"data": data})
        if path.endswith("/api-keys"):
            return httpx.Response(200, json={"id": "k1", "token": "app-minted00000000"})
        return httpx.Response(404, json={})

    def console(self):
        http = httpx.Client(
            transport=httpx.MockTransport(self.handler),
            base_url="https://dify.test/console/api",
        )
        return DifyManagement(
            token="ey.FAKE.TOKEN", base_url="https://dify.test", http_client=http
        )


class TestListing:
    def test_apps_come_back_typed(self):
        apps = Dify().console().apps.list()
        assert apps[0] == App(
            id="a1", name="triage", mode="advanced-chat", description="d1"
        )

    def test_a_mode_narrows_the_list(self):
        dify = Dify()
        assert [a.name for a in dify.console().apps.list(mode="workflow")] == ["notes"]
        assert dify.params["mode"] == "workflow"

    def test_a_name_narrows_the_list(self):
        dify = Dify()
        dify.console().apps.list(name="tri")
        assert dify.params["name"] == "tri"

    def test_paging_is_passed_through(self):
        dify = Dify()
        dify.console().apps.list(page=3, limit=5)
        assert dify.params["page"] == "3"
        assert dify.params["limit"] == "5"


class TestFindingOne:
    def test_by_id(self):
        assert Dify().console().apps.retrieve("a1").name == "triage"

    def test_by_exact_name(self):
        assert Dify().console().apps.retrieve("triage").id == "a1"

    def test_a_name_that_matches_nothing_says_how_to_look(self):
        with pytest.raises(ValidationError, match="management.apps.list"):
            Dify().console().apps.retrieve("nope")

    def test_an_ambiguous_name_lists_the_ids_rather_than_guessing(self):
        with pytest.raises(ValidationError) as caught:
            Dify().console().apps.retrieve("notes")
        assert "a2" in str(caught.value)
        assert "a3" in str(caught.value)


class TestOpeningOne:
    def test_it_carries_the_apps_own_mode(self):
        """The mode decides the Service API route, so a wrong one fails to run."""
        assert Dify().console().apps.open("triage").mode == "advanced-chat"

    def test_it_mints_a_key_because_dify_never_reveals_one_twice(self):
        assert Dify().console().apps.open("triage").api_key == "app-minted00000000"

    def test_a_key_you_already_have_is_used_instead(self):
        app = Dify().console().apps.open("triage", api_key="app-mine0000000000")
        assert app.api_key == "app-mine0000000000"

    def test_an_app_you_did_not_create_is_never_marked_created(self):
        """Apps this SDK created get deleted automatically; an existing one must not."""
        assert Dify().console().apps.open("triage").created is False


class TestOpeningOneAsAnApp:
    """`client_for` asked Dify the mode and picked a class. There is one class
    now, so what remains is checking the mode is real before handing it back."""

    def test_open_asks_before_returning(self):
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={"name": "a", "mode": "workflow"})

        DifyApp.open(
            "app-key",
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler), base_url="https://dify.test/v1"
            ),
        )
        assert seen == ["/v1/info"]

    def test_the_deployed_app_hands_back_a_dify_app(self):
        """One class now, so DeployedApp hands back the app itself."""
        deployed = Dify().console().apps.open("triage")
        assert isinstance(deployed.client(), DifyApp)
