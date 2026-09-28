"""Tests for API key resolution and for keeping keys out of rendered output."""

import pytest

from dify_client._async_transport import AsyncTransport
from dify_client._transport import Transport
from dify_client.exceptions import ValidationError
from dify_client.knowledge import DifyKnowledge
from dify_client.secrets import (
    API_KEY_ENV,
    BASE_URL_ENV,
    HOST_ENV,
    SecretKey,
    mask_secret,
    resolve_api_key,
    resolve_base_url,
    resolve_host,
)

KEY = "app-SECRETKEY123456"


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """Never let the developer's own environment decide a test's outcome."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.delenv(BASE_URL_ENV, raising=False)
    monkeypatch.delenv(HOST_ENV, raising=False)


class TestMasking:
    def test_it_keeps_the_type_prefix_and_the_last_four(self):
        assert mask_secret("app-SECRETKEY123456") == "app-****3456"
        assert mask_secret("ds-SECRETKEY123456") == "ds-****3456"

    def test_a_key_without_a_prefix_still_masks(self):
        assert mask_secret("nodashkey1234") == "****1234"

    def test_a_short_value_is_masked_completely(self):
        """Showing the tail of a short value would reveal most of it."""
        assert mask_secret("short") == "****"
        assert mask_secret("app-12") == "app-****"
        assert mask_secret("") == "****"

    def test_the_masked_form_never_contains_the_secret_tail_in_full(self):
        masked = mask_secret(KEY)
        assert KEY not in masked
        assert "SECRETKEY" not in masked


class TestSecretKey:
    def test_it_does_not_render_itself(self):
        secret = SecretKey(KEY)
        assert KEY not in repr(secret)
        assert KEY not in str(secret)
        assert repr(secret) == "SecretKey('app-****3456')"

    def test_reveal_returns_the_key(self):
        assert SecretKey(KEY).reveal() == KEY

    def test_a_provider_is_resolved_on_every_use(self):
        """A rotating key must not be cached at construction time."""
        seen = []

        def provider():
            seen.append(1)
            return f"app-key{len(seen)}"

        secret = SecretKey(provider)
        assert secret.reveal() == "app-key1"
        assert secret.reveal() == "app-key2"

    def test_rendering_a_provider_does_not_call_it(self):
        """Printing an object must not reach out to a vault."""

        def provider():
            raise AssertionError("the provider was called just to render")

        assert repr(SecretKey(provider)) == "SecretKey(<provider>)"

    def test_a_provider_returning_nothing_is_an_error(self):
        with pytest.raises(ValueError, match="returned an empty key"):
            SecretKey(lambda: "").reveal()

    def test_it_reports_whether_it_holds_or_produces(self):
        assert not SecretKey(KEY).is_provider
        assert SecretKey(lambda: KEY).is_provider


class TestResolution:
    def test_an_explicit_key_wins(self, monkeypatch):
        monkeypatch.setenv(API_KEY_ENV, "app-from-the-environment")
        assert resolve_api_key(KEY).reveal() == KEY

    def test_the_environment_is_the_fallback(self, monkeypatch):
        monkeypatch.setenv(API_KEY_ENV, KEY)
        assert resolve_api_key().reveal() == KEY

    def test_nothing_anywhere_names_both_ways_out(self):
        with pytest.raises(ValueError) as caught:
            resolve_api_key()
        assert "api_key=" in str(caught.value)
        assert API_KEY_ENV in str(caught.value)

    def test_an_explicit_empty_key_does_not_fall_back(self, monkeypatch):
        """A blank in code is a mistake, not a request to use the shell's key."""
        monkeypatch.setenv(API_KEY_ENV, KEY)
        with pytest.raises(ValueError, match="empty"):
            resolve_api_key("")

    def test_base_url_precedence(self, monkeypatch):
        assert resolve_base_url(None, "https://default") == "https://default"
        monkeypatch.setenv(BASE_URL_ENV, "https://from-env/v1")
        assert resolve_base_url(None, "https://default") == "https://from-env/v1"
        assert (
            resolve_base_url("https://explicit/v1", "https://default")
            == "https://explicit/v1"
        )

    def test_trailing_slashes_are_trimmed(self):
        assert resolve_base_url("https://x/v1/", "https://default") == "https://x/v1"

    def test_the_service_base_is_derived_from_the_host(self, monkeypatch):
        """One variable configures a self-hosted Dify, and difyctl reads it too."""
        monkeypatch.setenv(HOST_ENV, "http://localhost:8088")
        assert resolve_base_url(None, "https://default") == "http://localhost:8088/v1"

    def test_an_explicit_base_url_env_beats_the_host(self, monkeypatch):
        monkeypatch.setenv(HOST_ENV, "http://localhost:8088")
        monkeypatch.setenv(BASE_URL_ENV, "http://elsewhere/v2")
        assert resolve_base_url(None, "https://default") == "http://elsewhere/v2"

    def test_a_host_with_a_trailing_slash_still_derives_cleanly(self, monkeypatch):
        monkeypatch.setenv(HOST_ENV, "http://localhost:8088/")
        assert resolve_base_url(None, "https://default") == "http://localhost:8088/v1"

    def test_the_host_resolves_on_its_own(self, monkeypatch):
        monkeypatch.setenv(HOST_ENV, "http://localhost:8088")
        assert resolve_host(None, "https://cloud.dify.ai") == "http://localhost:8088"
        assert resolve_host("http://given", "https://cloud.dify.ai") == "http://given"


@pytest.mark.parametrize("client_class", [Transport, AsyncTransport, DifyKnowledge])
class TestClients:
    def test_an_explicit_key_is_accepted(self, client_class):
        assert client_class(KEY).api_key == KEY

    def test_the_key_can_come_from_the_environment(self, client_class, monkeypatch):
        monkeypatch.setenv(API_KEY_ENV, KEY)
        assert client_class().api_key == KEY

    def test_no_key_anywhere_raises_a_client_error(self, client_class):
        with pytest.raises(ValidationError, match=API_KEY_ENV):
            client_class()

    def test_repr_does_not_leak_the_key(self, client_class):
        client = client_class(KEY)
        assert KEY not in repr(client)
        assert "app-****3456" in repr(client)

    def test_the_instance_dict_does_not_leak_the_key(self, client_class):
        """Tracebacks with locals and error reporters read __dict__."""
        client = client_class(KEY)
        assert KEY not in repr(vars(client))

    def test_a_callable_key_is_resolved_per_use(self, client_class):
        keys = iter(["app-first000000000", "app-second00000000"])
        client = client_class(api_key=lambda: next(keys))
        assert client.api_key == "app-first000000000"
        assert client.api_key == "app-second00000000"

    def test_base_url_comes_from_the_environment(self, client_class, monkeypatch):
        monkeypatch.setenv(BASE_URL_ENV, "https://dify.internal/v1")
        assert client_class(KEY).base_url == "https://dify.internal/v1"

    def test_the_host_alone_configures_a_self_hosted_dify(
        self, client_class, monkeypatch
    ):
        monkeypatch.setenv(HOST_ENV, "https://dify.internal")
        assert client_class(KEY).base_url == "https://dify.internal/v1"

    def test_an_explicit_base_url_wins(self, client_class, monkeypatch):
        monkeypatch.setenv(BASE_URL_ENV, "https://dify.internal/v1")
        assert (
            client_class(KEY, base_url="https://other/v1").base_url
            == "https://other/v1"
        )
