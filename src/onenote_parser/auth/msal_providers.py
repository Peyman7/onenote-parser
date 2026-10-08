"""MSAL-backed token providers.

``msal`` is imported lazily so the package installs and runs (tests, offline
parsing of cached HTML, custom token brokers) without it.
"""

from __future__ import annotations

import atexit
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..errors import AuthenticationError
from ..logging_utils import get_logger
from .base import AccessToken, CachingTokenProvider

logger = get_logger("auth")

__all__ = [
    "DeviceCodeTokenProvider",
    "RefreshTokenProvider",
    "ClientCredentialsTokenProvider",
]

_MSAL_HINT = "MSAL is required for this auth mode: pip install 'onenote-parser[auth]'"


def _import_msal():  # type: ignore[no-untyped-def]
    try:
        import msal  # type: ignore
    except ImportError as exc:  # pragma: no cover - env dependent
        raise AuthenticationError(_MSAL_HINT) from exc
    return msal


def _build_cache(path: Optional[str]):  # type: ignore[no-untyped-def]
    """Serializable MSAL token cache persisted to ``path`` with 0600 perms."""
    msal = _import_msal()
    cache = msal.SerializableTokenCache()
    if not path:
        return cache, None
    cache_path = Path(path).expanduser()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        try:
            cache.deserialize(cache_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - corrupt cache
            logger.warning("Ignoring unreadable token cache %s: %s", cache_path, exc)

    def _persist() -> None:
        if cache.has_state_changed:
            cache_path.write_text(cache.serialize(), encoding="utf-8")
            try:
                os.chmod(cache_path, 0o600)
            except OSError:  # pragma: no cover - platform dependent
                pass

    atexit.register(_persist)
    return cache, _persist


def _token_from_result(result: dict[str, Any], scopes: Sequence[str]) -> AccessToken:
    if "access_token" not in result:
        raise AuthenticationError(
            result.get("error_description") or result.get("error") or "Token acquisition failed",
            details={"error": result.get("error"), "correlation_id": result.get("correlation_id")},
        )
    expires_in = result.get("expires_in")
    claims = result.get("id_token_claims") or {}
    return AccessToken(
        token=result["access_token"],
        expires_at=time.time() + float(expires_in) if expires_in else None,
        scopes=tuple(scopes),
        account=claims.get("preferred_username") or claims.get("upn"),
        claims=claims,
    )


class DeviceCodeTokenProvider(CachingTokenProvider):
    """Interactive delegated sign-in via the device-code flow.

    Suited to first-time setup of a service identity: sign in once, and the
    refresh token in the on-disk cache keeps subsequent headless runs working.
    """

    def __init__(
        self,
        client_id: str,
        *,
        tenant_id: str = "common",
        scopes: Sequence[str] = ("Notes.Read.All",),
        authority: Optional[str] = None,
        token_cache_path: Optional[str] = None,
        prompt_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        leeway_seconds: float = 300.0,
    ) -> None:
        super().__init__(leeway_seconds=leeway_seconds)
        if not client_id:
            raise AuthenticationError("DeviceCodeTokenProvider requires a client_id")
        self.client_id = client_id
        self.authority = authority or f"https://login.microsoftonline.com/{tenant_id}"
        # offline_access is implicit for MSAL public clients and must not be
        # passed in the scope list.
        self.scopes = [s for s in scopes if s.lower() not in {"offline_access", "openid", "profile"}]
        self.token_cache_path = token_cache_path
        self.prompt_callback = prompt_callback or self._default_prompt
        self._app = None
        self._persist: Optional[Callable[[], None]] = None

    @staticmethod
    def _default_prompt(flow: dict[str, Any]) -> None:  # pragma: no cover - interactive
        print(flow.get("message", "Complete the device-code sign-in in your browser."), flush=True)

    def _get_app(self):  # type: ignore[no-untyped-def]
        if self._app is None:
            msal = _import_msal()
            cache, persist = _build_cache(self.token_cache_path)
            self._persist = persist
            self._app = msal.PublicClientApplication(
                self.client_id, authority=self.authority, token_cache=cache
            )
        return self._app

    def _acquire(self) -> AccessToken:
        app = self._get_app()
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(self.scopes, account=accounts[0])
            if result and "access_token" in result:
                logger.debug("Acquired token silently from cache")
                if self._persist:
                    self._persist()
                return _token_from_result(result, self.scopes)
        flow = app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise AuthenticationError(
                "Failed to start the device-code flow",
                details={"error": flow.get("error"), "description": flow.get("error_description")},
            )
        self.prompt_callback(flow)
        result = app.acquire_token_by_device_flow(flow)
        if self._persist:
            self._persist()
        return _token_from_result(result, self.scopes)

    def export_refresh_token(self) -> Optional[str]:
        """Return the cached refresh token so it can be stored in a vault.

        This is how you bootstrap unattended runs: sign in once interactively,
        export the refresh token, then use :class:`RefreshTokenProvider`.
        """
        app = self._get_app()
        cache = app.token_cache
        entries = cache.find("RefreshToken")  # type: ignore[attr-defined]
        return entries[0].get("secret") if entries else None


class RefreshTokenProvider(CachingTokenProvider):
    """Headless delegated auth from a stored refresh token.

    This is the supported production mode for OneNote: a dedicated service
    account signs in once, and the refresh token (kept in Key Vault or an
    equivalent secret store) drives every scheduled run.
    """

    def __init__(
        self,
        client_id: str,
        refresh_token: str,
        *,
        client_secret: Optional[str] = None,
        tenant_id: str = "common",
        scopes: Sequence[str] = ("Notes.Read.All",),
        authority: Optional[str] = None,
        on_new_refresh_token: Optional[Callable[[str], None]] = None,
        leeway_seconds: float = 300.0,
    ) -> None:
        super().__init__(leeway_seconds=leeway_seconds)
        if not client_id:
            raise AuthenticationError("RefreshTokenProvider requires a client_id")
        if not refresh_token:
            raise AuthenticationError("RefreshTokenProvider requires a refresh_token")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.authority = authority or f"https://login.microsoftonline.com/{tenant_id}"
        self.scopes = [s for s in scopes if s.lower() not in {"offline_access", "openid", "profile"}]
        self.on_new_refresh_token = on_new_refresh_token
        self._app = None

    def _get_app(self):  # type: ignore[no-untyped-def]
        if self._app is None:
            msal = _import_msal()
            if self.client_secret:
                self._app = msal.ConfidentialClientApplication(
                    self.client_id, authority=self.authority, client_credential=self.client_secret
                )
            else:
                self._app = msal.PublicClientApplication(self.client_id, authority=self.authority)
        return self._app

    def _acquire(self) -> AccessToken:
        app = self._get_app()
        result = app.acquire_token_by_refresh_token(self.refresh_token, scopes=self.scopes)
        token = _token_from_result(result, self.scopes)
        # Entra rotates refresh tokens; persist the new one or the next
        # scheduled run fails with invalid_grant.
        new_refresh = result.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = new_refresh
            if self.on_new_refresh_token:
                self.on_new_refresh_token(new_refresh)
        return token


class ClientCredentialsTokenProvider(CachingTokenProvider):
    """App-only auth.

    .. warning::
       Microsoft retired app-only access to the **OneNote** Graph endpoints on
       2025-03-31.  Calls will return 401/403 regardless of granted application
       permissions.  This provider remains for adjacent Graph calls and for
       tenants operating under a documented exception.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tenant_id: str,
        *,
        scopes: Sequence[str] = ("https://graph.microsoft.com/.default",),
        authority: Optional[str] = None,
        leeway_seconds: float = 300.0,
    ) -> None:
        super().__init__(leeway_seconds=leeway_seconds)
        if not (client_id and client_secret and tenant_id):
            raise AuthenticationError(
                "ClientCredentialsTokenProvider requires client_id, client_secret and tenant_id"
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.authority = authority or f"https://login.microsoftonline.com/{tenant_id}"
        self.scopes = list(scopes) or ["https://graph.microsoft.com/.default"]
        self._app = None

    def _get_app(self):  # type: ignore[no-untyped-def]
        if self._app is None:
            msal = _import_msal()
            self._app = msal.ConfidentialClientApplication(
                self.client_id, authority=self.authority, client_credential=self.client_secret
            )
        return self._app

    def _acquire(self) -> AccessToken:
        logger.warning(
            "Using app-only auth against OneNote; Microsoft retired this on 2025-03-31 "
            "and requests are expected to fail with 401/403."
        )
        result = self._get_app().acquire_token_for_client(scopes=self.scopes)
        return _token_from_result(result, self.scopes)
