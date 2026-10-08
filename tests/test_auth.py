"""Token providers."""

from __future__ import annotations

import threading
import time

import pytest

from onenote_parser.auth import (
    CachingTokenProvider,
    CallableTokenProvider,
    RefreshTokenProvider,
    StaticTokenProvider,
    TokenProvider,
    create_token_provider,
)
from onenote_parser.auth.base import AccessToken
from onenote_parser.auth.msal_providers import _token_from_result
from onenote_parser.config import ParserConfig
from onenote_parser.errors import AuthenticationError, ConfigurationError


def test_static_provider_returns_the_token():
    provider = StaticTokenProvider("abc")
    assert isinstance(provider, TokenProvider)
    assert provider.get_token() == "abc"
    assert "abc" not in repr(provider)


def test_static_provider_rejects_empty_and_expired_tokens():
    with pytest.raises(AuthenticationError):
        StaticTokenProvider("")
    expired = StaticTokenProvider("abc", expires_at=time.time() - 10)
    with pytest.raises(AuthenticationError):
        expired.get_token()


def test_callable_provider_accepts_strings_and_access_tokens():
    assert CallableTokenProvider(lambda: "tok").get_token() == "tok"
    assert CallableTokenProvider(lambda: AccessToken("tok2")).get_token() == "tok2"
    with pytest.raises(AuthenticationError):
        CallableTokenProvider(lambda: "").get_token()


class CountingProvider(CachingTokenProvider):
    def __init__(self, ttl=3600.0, **kwargs):
        super().__init__(**kwargs)
        self.calls = 0
        self.ttl = ttl

    def _acquire(self) -> AccessToken:
        self.calls += 1
        time.sleep(0.01)
        return AccessToken(token=f"token-{self.calls}", expires_at=time.time() + self.ttl)


def test_caching_provider_acquires_once_for_concurrent_callers():
    provider = CountingProvider(leeway_seconds=0)
    tokens = []
    threads = [threading.Thread(target=lambda: tokens.append(provider.get_token()))
               for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert provider.calls == 1
    assert set(tokens) == {"token-1"}


def test_caching_provider_refreshes_when_expired_or_invalidated():
    provider = CountingProvider(ttl=0.0, leeway_seconds=0)
    assert provider.get_token() == "token-1"
    assert provider.get_token() == "token-2"  # already expired

    fresh = CountingProvider(leeway_seconds=0)
    assert fresh.get_token() == "token-1"
    assert fresh.get_token() == "token-1"
    fresh.invalidate()
    assert fresh.get_token() == "token-2"


def test_caching_provider_rejects_an_empty_acquisition():
    class Empty(CachingTokenProvider):
        def _acquire(self):
            return AccessToken(token="")

    with pytest.raises(AuthenticationError):
        Empty().get_token()


def test_token_result_parsing_and_error_surface():
    token = _token_from_result(
        {"access_token": "t", "expires_in": 3600,
         "id_token_claims": {"preferred_username": "svc@contoso.com"}},
        ["Notes.Read.All"],
    )
    assert token.token == "t" and token.account == "svc@contoso.com"
    assert token.expires_at > time.time()

    with pytest.raises(AuthenticationError) as excinfo:
        _token_from_result({"error": "invalid_grant",
                            "error_description": "refresh token expired"}, [])
    assert "refresh token expired" in str(excinfo.value)


def test_refresh_token_rotation_is_surfaced_to_the_caller():
    """Entra rotates refresh tokens; a run that ignores the new one breaks next time."""
    rotated: list[str] = []
    provider = RefreshTokenProvider("client", "old-rt", tenant_id="t",
                                    on_new_refresh_token=rotated.append)

    class FakeApp:
        def acquire_token_by_refresh_token(self, refresh_token, scopes):
            assert refresh_token == "old-rt"
            return {"access_token": "at", "expires_in": 3600, "refresh_token": "new-rt"}

    provider._app = FakeApp()
    assert provider.get_token() == "at"
    assert provider.refresh_token == "new-rt"
    assert rotated == ["new-rt"]


def test_refresh_token_provider_requires_credentials():
    with pytest.raises(AuthenticationError):
        RefreshTokenProvider("", "rt")
    with pytest.raises(AuthenticationError):
        RefreshTokenProvider("client", "")


def test_offline_access_is_not_passed_as_an_explicit_scope():
    """MSAL rejects reserved scopes; the config default includes offline_access."""
    provider = RefreshTokenProvider("client", "rt",
                                    scopes=("Notes.Read.All", "offline_access", "openid"))
    assert provider.scopes == ["Notes.Read.All"]


def test_factory_builds_the_configured_provider():
    config = ParserConfig()
    config.auth.mode = "static"
    config.auth.access_token = "tok"
    assert create_token_provider(config).get_token() == "tok"

    config.auth.mode = "device_code"
    config.auth.client_id = "app"
    assert type(create_token_provider(config)).__name__ == "DeviceCodeTokenProvider"

    config.auth.mode = "refresh_token"
    config.auth.refresh_token = "rt"
    assert type(create_token_provider(config)).__name__ == "RefreshTokenProvider"

    config.auth.mode = "nonsense"
    with pytest.raises(ConfigurationError):
        create_token_provider(config)
