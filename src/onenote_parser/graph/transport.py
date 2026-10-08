"""HTTP transport abstraction.

:class:`GraphClient` talks to a :class:`Transport`, never to ``requests``
directly.  That keeps retry/throttle logic fully testable without a network and
lets hosts swap in their own HTTP stack (httpx, aiohttp bridge, an
instrumented session).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from ..errors import TransportError

__all__ = ["Response", "Transport", "RequestsTransport"]


class _CaseInsensitiveDict(dict):
    """Minimal case-insensitive mapping for HTTP headers."""

    def __init__(self, data: Optional[Mapping[str, str]] = None) -> None:
        super().__init__()
        for key, value in (data or {}).items():
            self[key] = value

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key.lower(), value)

    def __getitem__(self, key: str) -> str:
        return super().__getitem__(key.lower())

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return super().get(key.lower(), default)

    def __contains__(self, key: object) -> bool:  # type: ignore[override]
        return super().__contains__(str(key).lower())


@dataclass(slots=True)
class Response:
    """A transport-agnostic HTTP response."""

    status_code: int
    content: bytes = b""
    headers: _CaseInsensitiveDict = field(default_factory=_CaseInsensitiveDict)
    url: str = ""
    encoding: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.headers, _CaseInsensitiveDict):
            self.headers = _CaseInsensitiveDict(self.headers)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    def text(self) -> str:
        charset = self.encoding
        if not charset:
            content_type = self.headers.get("content-type") or ""
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].split(";")[0].strip()
        return self.content.decode(charset or "utf-8", errors="replace")

    def json(self) -> Any:
        import json

        if not self.content:
            return None
        return json.loads(self.text())


@runtime_checkable
class Transport(Protocol):
    """The single method :class:`GraphClient` needs from an HTTP stack."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        timeout: Optional[tuple[float, float]] = None,
    ) -> Response:
        ...

    def close(self) -> None:
        ...


class RequestsTransport:
    """Default transport backed by a pooled ``requests.Session``."""

    def __init__(
        self,
        *,
        verify: bool = True,
        proxies: Optional[Mapping[str, str]] = None,
        pool_maxsize: int = 16,
        session: Any = None,
    ) -> None:
        import requests
        from requests.adapters import HTTPAdapter

        self._session = session or requests.Session()
        if session is None:
            adapter = HTTPAdapter(pool_connections=pool_maxsize, pool_maxsize=pool_maxsize,
                                  max_retries=0)
            self._session.mount("https://", adapter)
            self._session.mount("http://", adapter)
        self._session.verify = verify
        if proxies:
            self._session.proxies.update(dict(proxies))

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        timeout: Optional[tuple[float, float]] = None,
    ) -> Response:
        import requests

        try:
            raw = self._session.request(
                method,
                url,
                headers=dict(headers or {}),
                params=dict(params or {}) or None,
                timeout=timeout,
                allow_redirects=True,
            )
        except requests.exceptions.RequestException as exc:
            raise TransportError(f"{method} {url} failed: {exc}", details={"url": url}) from exc
        return Response(
            status_code=raw.status_code,
            content=raw.content,
            headers=_CaseInsensitiveDict(dict(raw.headers)),
            url=str(raw.url),
            encoding=raw.encoding,
        )

    def close(self) -> None:
        self._session.close()
