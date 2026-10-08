"""Retry, throttle, paging and error-mapping behaviour of the Graph client."""

from __future__ import annotations

import json

import pytest

from onenote_parser.config import GraphConfig
from onenote_parser.errors import (
    AccessDeniedError,
    GraphHTTPError,
    NotFoundError,
    ThrottledError,
    TransportError,
)
from onenote_parser.graph.client import GraphClient, RateLimiter
from onenote_parser.graph.transport import Response, _CaseInsensitiveDict

from conftest import StubTokenProvider


class ScriptedTransport:
    """Replays a fixed list of responses (or raises the queued exception)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers=None, params=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "params": dict(params or {})})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def json_response(payload: str, status: int = 200, headers=None, url="https://graph/test"):
    return Response(status, payload.encode(),
                    _CaseInsensitiveDict({"Content-Type": "application/json", **(headers or {})}),
                    url)


def make_client(responses, *, config=None, provider=None):
    slept: list[float] = []
    client = GraphClient(
        provider or StubTokenProvider(),
        config or GraphConfig(max_retries=3, backoff_initial_seconds=0.01,
                              default_retry_after_seconds=0.01),
        transport=ScriptedTransport(responses),
        sleep=slept.append,
    )
    return client, slept


def test_bearer_token_and_user_agent_are_sent():
    client, _ = make_client([json_response('{"value":[]}')])
    client.get_json("/onenote/notebooks")
    headers = client._transport.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer test-token"
    assert "onenote-parser" in headers["User-Agent"]


def test_retries_429_then_succeeds_and_counts_stats():
    client, slept = make_client([
        json_response('{"error":{"code":"TooManyRequests"}}', 429),
        json_response('{"value":[{"id":"1"}]}'),
    ])
    assert client.get_json("/onenote/notebooks")["value"] == [{"id": "1"}]
    assert client.stats.throttled == 1
    assert client.stats.retries == 1
    assert len(slept) == 1


def test_retry_after_header_is_honoured():
    client, slept = make_client([
        json_response("", 503, headers={"Retry-After": "7"}),
        json_response('{"value":[]}'),
    ])
    client.get_json("/onenote/notebooks")
    assert slept == [7.0]


def test_missing_retry_after_falls_back_to_configured_floor():
    """OneNote routinely returns 429 with no Retry-After."""
    config = GraphConfig(max_retries=2, default_retry_after_seconds=10.0,
                         backoff_multiplier=2.0, backoff_max_seconds=60.0)
    client, slept = make_client([
        json_response("", 429),
        json_response('{"value":[]}'),
    ], config=config)
    client.get_json("/onenote/notebooks")
    assert 7.5 <= slept[0] <= 12.5  # 10s floor with +-25% jitter


def test_exhausted_retries_raise_throttled_error():
    config = GraphConfig(max_retries=1, default_retry_after_seconds=0.01)
    client, _ = make_client([json_response("", 429), json_response("", 429)], config=config)
    with pytest.raises(ThrottledError) as excinfo:
        client.get_json("/onenote/notebooks")
    assert excinfo.value.status_code == 429


def test_401_triggers_exactly_one_token_refresh_and_replay():
    provider = StubTokenProvider()
    client, _ = make_client([
        json_response('{"error":{"code":"InvalidAuthenticationToken"}}', 401),
        json_response('{"value":[]}'),
    ], provider=provider)
    client.get_json("/onenote/notebooks")
    assert provider.invalidated == 1
    assert client._transport.calls[1]["headers"]["Authorization"] == "Bearer refreshed-token"


def test_second_401_is_not_retried_again():
    provider = StubTokenProvider()
    client, _ = make_client([json_response("", 401), json_response("", 401)], provider=provider)
    with pytest.raises(AccessDeniedError):
        client.get_json("/onenote/notebooks")
    assert provider.invalidated == 1


def test_404_maps_to_not_found_and_allow_404_returns_none():
    client, _ = make_client([json_response('{"error":{"code":"ItemNotFound"}}', 404)])
    with pytest.raises(NotFoundError):
        client.get_json("/onenote/pages/x")

    client, _ = make_client([json_response('{"error":{"code":"ItemNotFound"}}', 404)])
    assert client.get_json("/onenote/pages/x", allow_404=True) is None


def test_error_details_carry_graph_code_and_request_id():
    client, _ = make_client([
        json_response('{"error":{"code":"BadRequest","message":"bad $select"}}', 400,
                      headers={"request-id": "abc-123"}),
    ])
    with pytest.raises(GraphHTTPError) as excinfo:
        client.get_json("/onenote/notebooks")
    assert excinfo.value.graph_code == "BadRequest"
    assert excinfo.value.request_id == "abc-123"
    assert "bad $select" in excinfo.value.message


def test_transport_errors_are_retried_then_reraised():
    client, slept = make_client([
        TransportError("connection reset"),
        json_response('{"value":[]}'),
    ])
    client.get_json("/onenote/notebooks")
    assert len(slept) == 1

    config = GraphConfig(max_retries=0)
    client, _ = make_client([TransportError("dns failure")], config=config)
    with pytest.raises(TransportError):
        client.get_json("/onenote/notebooks")


def test_paging_follows_next_link_and_respects_max_items():
    responses = [
        json_response('{"value":[{"id":"1"},{"id":"2"}],'
                      '"@odata.nextLink":"https://graph/next"}'),
        json_response('{"value":[{"id":"3"}]}'),
    ]
    client, _ = make_client(responses)
    assert [i["id"] for i in client.iter_paged("/onenote/pages")] == ["1", "2", "3"]

    client, _ = make_client([json_response('{"value":[{"id":"1"},{"id":"2"}],'
                                           '"@odata.nextLink":"https://graph/next"}')])
    assert [i["id"] for i in client.iter_paged("/onenote/pages", max_items=1)] == ["1"]


def test_top_defaults_to_page_size_and_is_not_resent_on_next_link():
    responses = [
        json_response('{"value":[],"@odata.nextLink":"https://graph/next"}'),
        json_response('{"value":[]}'),
    ]
    client, _ = make_client(responses, config=GraphConfig(page_size=25))
    list(client.iter_paged("/onenote/pages"))
    assert client._transport.calls[0]["params"]["$top"] == 25
    assert client._transport.calls[1]["params"] == {}


@pytest.mark.parametrize(
    "target,path,expected",
    [
        ("me", "/onenote/notebooks", "https://graph.microsoft.com/v1.0/me/onenote/notebooks"),
        ("users/svc@x.com", "/onenote/pages",
         "https://graph.microsoft.com/v1.0/users/svc@x.com/onenote/pages"),
        ("groups/g1", "onenote/sections", "https://graph.microsoft.com/v1.0/groups/g1/onenote/sections"),
        ("me", "https://graph/next", "https://graph/next"),
        ("me", "/users/u1/drive", "https://graph.microsoft.com/v1.0/users/u1/drive"),
    ],
)
def test_build_url_scopes_onenote_paths_to_the_target(target, path, expected):
    client, _ = make_client([], config=GraphConfig(target=target))
    assert client.build_url(path) == expected


def test_page_ids_with_special_characters_are_escaped():
    client, _ = make_client([])
    assert client.escape("1-abc!123") == "1-abc%21123"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_rate_limiter_enforces_sustained_rate():
    clock = FakeClock()
    limiter = RateLimiter(60, sleep=clock.sleep, clock=clock)  # 1/second, burst of 60
    for _ in range(60):
        assert limiter.acquire() == 0.0  # burst capacity
    assert limiter.acquire() == pytest.approx(1.0, abs=0.01)  # then throttled to the rate


def test_get_bytes_returns_content_and_headers():
    body = b"\x89PNG binary"
    client, _ = make_client([Response(200, body, _CaseInsensitiveDict({"Content-Type": "image/png"}),
                                      "https://graph/res")])
    data, headers = client.get_bytes("https://graph/res")
    assert data == body
    assert headers.get("content-type") == "image/png"
    assert client.stats.bytes_downloaded == len(body)


# -- resource URL repair ----------------------------------------------------- #


SITE_TARGET = "sites/contoso.sharepoint.com,aaaa-bbbb,cccc-dddd"
RESOURCE_ID = "1-e50444bdfbdc4ab7ac89a84675bc1358!1-04ccc780-03d7-4ffb"


def test_legacy_sitecollections_resource_url_is_rewritten():
    """OneNote emits siteCollections/... URLs that Graph v1.0 rejects (400)."""
    client, _ = make_client([], config=GraphConfig(target=SITE_TARGET))
    embedded = (
        "https://graph.microsoft.com/v1.0/siteCollections/"
        f"contoso.sharepoint.com,aaaa-bbbb,cccc-dddd/onenote/resources/{RESOURCE_ID}/$value"
    )
    assert client.resource_url(embedded) == (
        f"https://graph.microsoft.com/v1.0/{SITE_TARGET}/onenote/resources/{RESOURCE_ID}/$value"
    )


def test_resource_url_accepts_a_bare_resource_id():
    client, _ = make_client([], config=GraphConfig(target="me"))
    assert client.resource_url(RESOURCE_ID) == (
        f"https://graph.microsoft.com/v1.0/me/onenote/resources/{RESOURCE_ID}/$value"
    )


def test_resource_url_is_a_no_op_for_personal_notebook_urls():
    client, _ = make_client([], config=GraphConfig(target="me"))
    url = f"https://graph.microsoft.com/v1.0/me/onenote/resources/{RESOURCE_ID}/$value"
    assert client.resource_url(url) == url


def test_resource_url_never_rewrites_external_hosts():
    """An externally hosted image must not be re-pointed at the tenant."""
    client, _ = make_client([], config=GraphConfig(target=SITE_TARGET))
    for url in ("https://cdn.example.com/resources/abc/$value",
                "https://example.com/image.png", ""):
        assert client.resource_url(url) == url


# -- $skip paging (OneNote collections omit @odata.nextLink) ----------------- #


def page_of(count: int, start: int = 0, next_link: str | None = None):
    payload = {"value": [{"id": f"p{start + i}"} for i in range(count)]}
    if next_link:
        payload["@odata.nextLink"] = next_link
    return json_response(json.dumps(payload))


def test_full_page_without_nextlink_continues_with_skip():
    """Regression: OneNote caps /pages at $top and returns no continuation link."""
    client, _ = make_client([page_of(3, 0), page_of(3, 3), page_of(1, 6)],
                            config=GraphConfig(page_size=3))
    items = [i["id"] for i in client.iter_paged("/onenote/sections/s1/pages")]
    assert items == ["p0", "p1", "p2", "p3", "p4", "p5", "p6"]
    assert client._transport.calls[0]["params"]["$top"] == 3
    assert "$skip" not in client._transport.calls[0]["params"]
    assert client._transport.calls[1]["params"]["$skip"] == 3
    assert client._transport.calls[2]["params"]["$skip"] == 6


def test_short_first_page_issues_no_extra_request():
    client, _ = make_client([page_of(2)], config=GraphConfig(page_size=3))
    assert len([*client.iter_paged("/onenote/sections/s1/pages")]) == 2
    assert len(client._transport.calls) == 1


def test_exactly_full_last_page_is_confirmed_by_an_empty_follow_up():
    client, _ = make_client([page_of(3, 0), page_of(0, 3)], config=GraphConfig(page_size=3))
    assert len([*client.iter_paged("/onenote/sections/s1/pages")]) == 3
    assert len(client._transport.calls) == 2


def test_service_that_ignores_skip_does_not_loop_forever():
    """Defensive: a replayed batch ends paging instead of spinning."""
    client, _ = make_client([page_of(2, 0)] * 6, config=GraphConfig(page_size=2))
    assert [i["id"] for i in client.iter_paged("/onenote/sections/s1/pages")] == ["p0", "p1"]
    assert len(client._transport.calls) == 2


def test_nextlink_takes_precedence_over_skip():
    client, _ = make_client(
        [page_of(3, 0, next_link="https://graph/next"), page_of(1, 3)],
        config=GraphConfig(page_size=3),
    )
    assert len([*client.iter_paged("/onenote/notebooks")]) == 4
    assert client._transport.calls[1]["url"] == "https://graph/next"
    assert client._transport.calls[1]["params"] == {}


def test_skip_paging_respects_max_items():
    client, _ = make_client([page_of(3, 0), page_of(3, 3)], config=GraphConfig(page_size=3))
    assert len([*client.iter_paged("/onenote/sections/s1/pages", max_items=4)]) == 4


def test_custom_top_is_used_as_the_skip_stride():
    client, _ = make_client([page_of(2, 0), page_of(1, 2)], config=GraphConfig(page_size=100))
    items = [*client.iter_paged("/onenote/sections/s1/pages", params={"$top": 2})]
    assert len(items) == 3
    assert client._transport.calls[1]["params"]["$skip"] == 2
