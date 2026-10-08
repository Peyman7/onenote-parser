"""Exception hierarchy for :mod:`onenote_parser`.

Every error raised by this package derives from :class:`OneNoteParserError` so
callers embedding the parser can catch one type and inspect the details.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = [
    "OneNoteParserError",
    "ConfigurationError",
    "AuthenticationError",
    "GraphError",
    "GraphHTTPError",
    "NotFoundError",
    "AccessDeniedError",
    "ThrottledError",
    "TransportError",
    "ParseError",
    "ResourceError",
    "ExportError",
    "StateError",
]


class OneNoteParserError(Exception):
    """Base class for all errors raised by the package."""

    def __init__(self, message: str, *, details: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.details:
            extras = ", ".join(f"{k}={v!r}" for k, v in sorted(self.details.items()))
            return f"{self.message} ({extras})"
        return self.message


class ConfigurationError(OneNoteParserError):
    """Invalid or incomplete configuration."""


class AuthenticationError(OneNoteParserError):
    """A token could not be acquired or is unusable."""


class GraphError(OneNoteParserError):
    """Base class for Microsoft Graph failures."""


class TransportError(GraphError):
    """Network-level failure (DNS, TLS, connection reset, timeout)."""


class GraphHTTPError(GraphError):
    """A non-successful HTTP response from Microsoft Graph."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        url: str,
        graph_code: Optional[str] = None,
        request_id: Optional[str] = None,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(
            message,
            details={
                "status_code": status_code,
                "url": url,
                "graph_code": graph_code,
                "request_id": request_id,
            },
        )
        self.status_code = status_code
        self.url = url
        self.graph_code = graph_code
        self.request_id = request_id
        self.body = body


class NotFoundError(GraphHTTPError):
    """HTTP 404 - the resource does not exist (or was deleted)."""


class AccessDeniedError(GraphHTTPError):
    """HTTP 401/403 - the signed-in identity may not read this resource."""


class ThrottledError(GraphHTTPError):
    """HTTP 429/503 that survived the retry budget."""

    def __init__(self, message: str, *, retry_after: Optional[float] = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after
        self.details["retry_after"] = retry_after


class ParseError(OneNoteParserError):
    """The page HTML could not be turned into content blocks."""


class ResourceError(OneNoteParserError):
    """An image or attachment could not be downloaded or processed."""


class ExportError(OneNoteParserError):
    """Normalized output could not be written."""


class StateError(OneNoteParserError):
    """The incremental-sync state store is unreadable or corrupt."""
