"""Build a :class:`TokenProvider` from :class:`~onenote_parser.config.AuthConfig`."""

from __future__ import annotations


from ..config import ParserConfig
from ..errors import ConfigurationError
from .base import StaticTokenProvider, TokenProvider
from .msal_providers import (
    ClientCredentialsTokenProvider,
    DeviceCodeTokenProvider,
    RefreshTokenProvider,
)

__all__ = ["create_token_provider"]


def create_token_provider(config: ParserConfig) -> TokenProvider:
    """Instantiate the provider selected by ``config.auth.mode``."""
    auth = config.auth
    if auth.mode == "static":
        if not auth.access_token:
            raise ConfigurationError("auth.mode='static' requires auth.access_token")
        return StaticTokenProvider(auth.access_token)
    if auth.mode == "device_code":
        return DeviceCodeTokenProvider(
            auth.client_id or "",
            tenant_id=auth.tenant_id,
            scopes=auth.scopes,
            authority=auth.authority,
            token_cache_path=auth.token_cache_path,
            leeway_seconds=auth.expiry_leeway_seconds,
        )
    if auth.mode == "refresh_token":
        return RefreshTokenProvider(
            auth.client_id or "",
            auth.refresh_token or "",
            client_secret=auth.client_secret,
            tenant_id=auth.tenant_id,
            scopes=auth.scopes,
            authority=auth.authority,
            leeway_seconds=auth.expiry_leeway_seconds,
        )
    if auth.mode == "client_credentials":
        return ClientCredentialsTokenProvider(
            auth.client_id or "",
            auth.client_secret or "",
            auth.tenant_id,
            authority=auth.authority,
            leeway_seconds=auth.expiry_leeway_seconds,
        )
    raise ConfigurationError(f"Unsupported auth.mode {auth.mode!r}")
