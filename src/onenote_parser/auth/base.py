"""Token provider protocol and dependency-free implementations."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from ..errors import AuthenticationError

__all__ = [
    "AccessToken",
    "TokenProvider",
    "StaticTokenProvider",
    "CallableTokenProvider",
    "CachingTokenProvider",
]


@dataclass(slots=True)
class AccessToken:
    """A bearer token plus the metadata needed to decide when to refresh it."""

    token: str
    expires_at: Optional[float] = None
    scopes: tuple[str, ...] = ()
    account: Optional[str] = None
    claims: Mapping[str, Any] = field(default_factory=dict)

    def is_expired(self, leeway_seconds: float = 300.0) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - leeway_seconds)


@runtime_checkable
class TokenProvider(Protocol):
    """Anything able to hand out a Graph bearer token."""

    def get_token(self) -> str:
        """Return a valid access token, refreshing transparently if needed."""
        ...


class StaticTokenProvider:
    """Wraps a token string obtained elsewhere (CI secret, broker, test)."""

    def __init__(self, token: str, *, expires_at: Optional[float] = None) -> None:
        if not token:
            raise AuthenticationError("StaticTokenProvider requires a non-empty token")
        self._token = AccessToken(token=token, expires_at=expires_at)

    def get_token(self) -> str:
        if self._token.is_expired(0):
            raise AuthenticationError(
                "The static access token has expired; supply a fresh one or use "
                "auth.mode='refresh_token'."
            )
        return self._token.token

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "StaticTokenProvider(token=***)"


class CallableTokenProvider:
    """Adapts a user-supplied callable, e.g. ``lambda: credential.get_token(...)``."""

    def __init__(self, factory: Callable[[], str | AccessToken]) -> None:
        self._factory = factory

    def get_token(self) -> str:
        value = self._factory()
        token = value.token if isinstance(value, AccessToken) else value
        if not token:
            raise AuthenticationError("Token factory returned an empty token")
        return token


class CachingTokenProvider:
    """Thread-safe cache in front of an expensive token acquisition.

    Subclasses implement :meth:`_acquire`.  Concurrent page fetches therefore
    trigger at most one refresh.
    """

    def __init__(self, *, leeway_seconds: float = 300.0) -> None:
        self._lock = threading.Lock()
        self._cached: Optional[AccessToken] = None
        self._leeway = leeway_seconds

    def _acquire(self) -> AccessToken:  # pragma: no cover - abstract
        raise NotImplementedError

    def get_token(self) -> str:
        cached = self._cached
        if cached is not None and not cached.is_expired(self._leeway):
            return cached.token
        with self._lock:
            cached = self._cached
            if cached is not None and not cached.is_expired(self._leeway):
                return cached.token
            token = self._acquire()
            if not token.token:
                raise AuthenticationError(f"{type(self).__name__} acquired an empty token")
            self._cached = token
            return token.token

    def invalidate(self) -> None:
        """Force the next :meth:`get_token` to re-acquire (used on a 401)."""
        with self._lock:
            self._cached = None
