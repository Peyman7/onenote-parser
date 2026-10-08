"""Token acquisition for Microsoft Graph.

The rest of the package only depends on the :class:`TokenProvider` protocol, so
host applications that already have an identity layer can plug their own token
source in with :class:`CallableTokenProvider` and skip MSAL entirely.
"""

from .base import (
    AccessToken,
    CallableTokenProvider,
    CachingTokenProvider,
    StaticTokenProvider,
    TokenProvider,
)
from .factory import create_token_provider
from .msal_providers import (
    ClientCredentialsTokenProvider,
    DeviceCodeTokenProvider,
    RefreshTokenProvider,
)

__all__ = [
    "AccessToken",
    "TokenProvider",
    "StaticTokenProvider",
    "CallableTokenProvider",
    "CachingTokenProvider",
    "DeviceCodeTokenProvider",
    "RefreshTokenProvider",
    "ClientCredentialsTokenProvider",
    "create_token_provider",
]
