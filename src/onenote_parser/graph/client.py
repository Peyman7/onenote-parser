"""Microsoft Graph client: rate limiting, retries, paging, resource download.

OneNote is one of the more aggressively throttled Graph workloads and it
frequently answers 429 **without** a ``Retry-After`` header, so the policy here
is deliberately conservative:

* a client-side token bucket keeps the request rate under the documented
  per-user ceiling before the service has to push back;
* retries use exponential backoff with full jitter, honouring ``Retry-After``
  when present and falling back to ``graph.default_retry_after_seconds``;
* 401 triggers exactly one token refresh + replay, so a token that expires
  mid-crawl does not fail the run.
"""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Optional
from urllib.parse import quote, urlparse

from ..config import GraphConfig, ParserConfig
from ..errors import (
    AccessDeniedError,
    GraphHTTPError,
    NotFoundError,
    ThrottledError,
    TransportError,
)
from ..logging_utils import get_logger
from .transport import RequestsTransport, Response, Transport

logger = get_logger("graph")

__all__ = ["GraphClient", "GraphStats", "RateLimiter"]

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 509})

#: Resource id inside a OneNote resource URL, whatever addressing precedes it.
_RESOURCE_ID_RE = re.compile(r"/resources/([^/?#]+)", re.IGNORECASE)


@dataclass
class GraphStats:
    """Counters exposed for observability and for :class:`RunStats`."""

    requests: int = 0
    retries: int = 0
    throttled: int = 0
    failures: int = 0
    bytes_downloaded: int = 0
    wait_seconds: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "throttled": self.throttled,
            "failures": self.failures,
            "bytes_downloaded": self.bytes_downloaded,
            "wait_seconds": round(self.wait_seconds, 3),
        }


class RateLimiter:
    """Thread-safe token bucket limiting requests per minute.

    Capacity equals one minute of budget, so short bursts are allowed while the
    sustained rate stays under the limit.
    """

    def __init__(self, requests_per_minute: int, *, sleep=time.sleep, clock=time.monotonic) -> None:
        self.rate_per_second = max(requests_per_minute, 1) / 60.0
        self.capacity = float(max(requests_per_minute, 1))
        self._tokens = self.capacity
        self._updated = clock()
        self._lock = threading.Lock()
        self._sleep = sleep
        self._clock = clock

    def acquire(self, amount: float = 1.0) -> float:
        """Block until ``amount`` tokens are available; return seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                elapsed = max(0.0, now - self._updated)
                self._updated = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
                if self._tokens >= amount:
                    self._tokens -= amount
                    return waited
                deficit = amount - self._tokens
                delay = deficit / self.rate_per_second
            waited += delay
            self._sleep(delay)


class GraphClient:
    """Thin, resilient wrapper over the Microsoft Graph REST surface."""

    def __init__(
        self,
        token_provider: Any,
        config: GraphConfig | ParserConfig | None = None,
        *,
        transport: Optional[Transport] = None,
        sleep=time.sleep,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        if isinstance(config, ParserConfig):
            config = config.graph
        self.config = config or GraphConfig()
        self.token_provider = token_provider
        self.stats = GraphStats()
        self._sleep = sleep
        self._transport = transport or RequestsTransport(
            verify=self.config.verify_tls, proxies=self.config.proxies
        )
        self._owns_transport = transport is None
        self._limiter = rate_limiter or RateLimiter(self.config.requests_per_minute, sleep=sleep)

    # -- lifecycle -------------------------------------------------------- #

    def close(self) -> None:
        if self._owns_transport:
            self._transport.close()

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- URL helpers ------------------------------------------------------ #

    def build_url(self, path: str) -> str:
        """Absolute URL for a Graph path.

        ``path`` may be absolute (``https://...``, e.g. an ``@odata.nextLink``),
        target-relative (``/onenote/notebooks``) or root-relative
        (``/users/x/onenote``) when it already starts with a known Graph root.
        """
        if path.startswith(("http://", "https://")):
            return path
        clean = path.lstrip("/")
        root = self.config.api_root
        if clean.startswith("onenote"):
            return f"{root}/{self.config.target_prefix}/{clean}"
        return f"{root}/{clean}"

    def resource_url(self, url_or_id: str) -> str:
        """Return a working URL for a OneNote resource (image or attachment).

        OneNote embeds absolute resource URLs in page HTML, but for
        site-hosted notebooks it emits a legacy
        ``/v1.0/siteCollections/{id}/onenote/resources/...`` address that Graph
        v1.0 rejects with ``BadRequest: Resource not found for the segment
        'siteCollections'``.  Rebuilding the URL from the resource id against
        the configured target fixes that and is a no-op for personal notebooks,
        whose embedded URLs already resolve.

        Non-Graph URLs are returned untouched, so an externally hosted image is
        never rewritten to point at the tenant.
        """
        if not url_or_id:
            return url_or_id
        if not url_or_id.startswith(("http://", "https://")):
            return self.build_url(f"/onenote/resources/{url_or_id}/$value")
        host = urlparse(url_or_id).hostname or ""
        if not host.endswith("graph.microsoft.com"):
            return url_or_id
        match = _RESOURCE_ID_RE.search(url_or_id)
        if not match:
            return url_or_id
        return self.build_url(f"/onenote/resources/{match.group(1)}/$value")

    @staticmethod
    def escape(value: str) -> str:
        """Percent-escape a path segment (ids may contain ``!`` and ``-``)."""
        return quote(value, safe="")

    # -- core request loop ------------------------------------------------ #

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        accept: str = "application/json",
        allow_404: bool = False,
    ) -> Response:
        """Issue one logical request, retrying per policy.

        Raises the mapped :class:`~onenote_parser.errors.GraphHTTPError`
        subclass on a terminal failure.
        """
        url = self.build_url(path)
        attempt = 0
        refreshed = False
        last_error: Optional[Exception] = None

        while attempt <= self.config.max_retries:
            self.stats.wait_seconds += self._limiter.acquire()
            request_headers = {
                "Authorization": f"Bearer {self.token_provider.get_token()}",
                "Accept": accept,
                "User-Agent": self.config.user_agent,
                **dict(headers or {}),
            }
            self.stats.requests += 1
            try:
                response = self._transport.request(
                    method,
                    url,
                    headers=request_headers,
                    params=params,
                    timeout=(self.config.connect_timeout_seconds, self.config.timeout_seconds),
                )
            except TransportError as exc:
                last_error = exc
                self.stats.failures += 1
                if attempt >= self.config.max_retries:
                    raise
                delay = self._backoff_delay(attempt)
                logger.warning("Transport error on %s %s (attempt %d): %s; retrying in %.1fs",
                               method, url, attempt + 1, exc, delay)
                self.stats.retries += 1
                self._sleep(delay)
                attempt += 1
                continue

            if response.ok:
                self.stats.bytes_downloaded += len(response.content)
                return response

            status = response.status_code

            if status == 401 and not refreshed:
                # Token expired mid-run: drop the cached token and replay once.
                refreshed = True
                invalidate = getattr(self.token_provider, "invalidate", None)
                if callable(invalidate):
                    logger.info("Received 401; refreshing access token and retrying once")
                    invalidate()
                    self.stats.retries += 1
                    continue

            if status == 404 and allow_404:
                return response

            if status in RETRYABLE_STATUS and attempt < self.config.max_retries:
                if status == 429:
                    self.stats.throttled += 1
                delay = self._retry_delay(response, attempt)
                logger.warning(
                    "Graph returned %d for %s %s (attempt %d/%d); backing off %.1fs",
                    status, method, url, attempt + 1, self.config.max_retries + 1, delay,
                )
                self.stats.retries += 1
                self._sleep(delay)
                attempt += 1
                continue

            self.stats.failures += 1
            raise self._map_error(response)

        self.stats.failures += 1
        if last_error:
            raise last_error
        raise GraphHTTPError(  # pragma: no cover - defensive
            f"{method} {url} exhausted retries", status_code=0, url=url
        )

    # -- convenience ------------------------------------------------------ #

    def get_json(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        allow_404: bool = False,
    ) -> Optional[dict[str, Any]]:
        response = self.request("GET", path, params=params, allow_404=allow_404)
        if response.status_code == 404:
            return None
        payload = response.json()
        return payload if isinstance(payload, dict) else {"value": payload}

    def get_bytes(self, path: str, *, accept: str = "*/*") -> tuple[bytes, Mapping[str, str]]:
        """Download a binary resource (image, attachment)."""
        response = self.request("GET", path, accept=accept)
        return response.content, response.headers

    def get_text(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        accept: str = "text/html",
    ) -> tuple[str, Mapping[str, str]]:
        """Download a text resource (page HTML)."""
        response = self.request("GET", path, params=params, accept=accept)
        return response.text(), response.headers

    def iter_paged(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        max_items: Optional[int] = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of a collection, exhausting all pages.

        Two paging styles are handled, because OneNote uses both:

        * **``@odata.nextLink``** - the standard Graph continuation, followed
          verbatim.
        * **``$skip`` offsets** - the OneNote ``/pages``, ``/sections`` and
          ``/notebooks`` collections cap a response at ``$top`` and return *no*
          continuation link, so a full page of results is indistinguishable
          from the end of the collection.  Trusting ``@odata.nextLink`` alone
          silently truncates any section with more than ``graph.page_size``
          pages.  When a full page arrives without a link, paging continues
          with ``$skip``.

        The ``$skip`` walk stops as soon as a short page arrives, and bails out
        if the service ignores ``$skip`` and replays the same batch.
        """
        base_query: dict[str, Any] = dict(params or {})
        base_query.setdefault("$top", self.config.page_size)
        try:
            top = int(base_query["$top"])
        except (TypeError, ValueError):
            top = self.config.page_size

        start_url = self.build_url(path)
        next_url: Optional[str] = start_url
        query: Optional[dict[str, Any]] = dict(base_query)
        emitted = 0
        requests_made = 0
        skip = 0
        previous_first_id: Optional[str] = None

        while next_url:
            payload = self.get_json(next_url, params=query) or {}
            items = payload.get("value") or []
            requests_made += 1

            first_id = str(items[0].get("id")) if items and isinstance(items[0], dict) else None
            if skip and first_id is not None and first_id == previous_first_id:
                # The service ignored $skip and replayed the previous batch;
                # emitting it would duplicate items and never terminate.
                logger.warning(
                    "%s ignored $skip (repeated batch); stopping after %d item(s)",
                    path, emitted,
                )
                return
            previous_first_id = first_id

            for item in items:
                yield item
                emitted += 1
                if max_items is not None and emitted >= max_items:
                    return

            link = payload.get("@odata.nextLink")
            if link:
                next_url, query = link, None
                continue

            if len(items) < top:
                return  # short page: the collection is exhausted

            skip += top
            next_url = start_url
            query = {**base_query, "$skip": skip}
            logger.debug("No nextLink for %s; continuing with $skip=%d", path, skip)

            if requests_made > self.config.max_page_fetch:  # pragma: no cover - runaway guard
                logger.error("Aborting paging after %d requests for %s", requests_made, path)
                return

    # -- internals -------------------------------------------------------- #

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter."""
        ceiling = min(
            self.config.backoff_max_seconds,
            self.config.backoff_initial_seconds * (self.config.backoff_multiplier ** attempt),
        )
        return random.uniform(0.0, max(ceiling, 0.001))

    def _retry_delay(self, response: Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), self.config.backoff_max_seconds)
            except (TypeError, ValueError):
                pass
        if response.status_code == 429:
            # OneNote 429s routinely omit Retry-After; use the configured floor
            # plus jitter so parallel workers do not resynchronize.
            base = self.config.default_retry_after_seconds * (
                self.config.backoff_multiplier ** attempt
            )
            return min(self.config.backoff_max_seconds, base) * random.uniform(0.75, 1.25)
        return self._backoff_delay(attempt)

    @staticmethod
    def _map_error(response: Response) -> GraphHTTPError:
        code: Optional[str] = None
        message = f"HTTP {response.status_code}"
        request_id = response.headers.get("request-id") or response.headers.get("client-request-id")
        body_text = response.text()[:2000]
        try:
            payload = response.json() or {}
            error = payload.get("error") or {}
            code = error.get("code")
            message = error.get("message") or message
        except Exception:  # noqa: BLE001 - non-JSON error bodies are common
            message = body_text or message

        kwargs = {
            "status_code": response.status_code,
            "url": response.url,
            "graph_code": code,
            "request_id": request_id,
            "body": body_text,
        }
        if response.status_code == 404:
            return NotFoundError(f"Not found: {message}", **kwargs)
        if response.status_code in (401, 403):
            return AccessDeniedError(f"Access denied: {message}", **kwargs)
        if response.status_code in (429, 503):
            retry_after = response.headers.get("retry-after")
            return ThrottledError(
                f"Throttled: {message}",
                retry_after=float(retry_after) if retry_after else None,
                **kwargs,
            )
        return GraphHTTPError(f"Graph request failed: {message}", **kwargs)
