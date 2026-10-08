"""Recursive hierarchy discovery, filtering and permission resilience."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from onenote_parser.config import ParserConfig
from onenote_parser.discovery import NameMatcher, NotebookCrawler, assign_parent_pages
from onenote_parser.graph.client import GraphClient
from onenote_parser.models import PageRef

from conftest import StubTokenProvider


def crawler(fake_graph, config: ParserConfig | None = None) -> NotebookCrawler:
    config = config or ParserConfig()
    client = GraphClient(StubTokenProvider(), config.graph, transport=fake_graph,
                         sleep=lambda _s: None)
    return NotebookCrawler(client, config)


def test_full_hierarchy_including_nested_section_groups(fake_graph):
    tree = crawler(fake_graph).crawl()
    assert tree.counts()["notebooks"] == 2
    assert tree.counts()["section_groups"] == 2  # Pumps -> Centrifugal
    names = [s.display_name for s in tree.iter_sections()]
    assert "Commissioning" in names and "Restricted" in names


def test_paths_are_recorded_at_every_level(fake_graph):
    tree = crawler(fake_graph).crawl()
    section = next(s for s in tree.iter_sections() if s.display_name == "Commissioning")
    assert section.path == ["Engineering", "Pumps", "Centrifugal", "Commissioning"]
    assert section.path_ids == ["nb-eng", "grp-pumps", "grp-centrifugal", "sec-commissioning"]
    assert section.pages[0].path == section.path


def test_pages_carry_ids_urls_and_timestamps(fake_graph):
    tree = crawler(fake_graph).crawl()
    page = next(p for p in tree.iter_pages() if p.id == "pg-2")
    assert page.notebook_id == "nb-eng" and page.section_id == "sec-commissioning"
    assert page.web_url == "https://onenote.com/pg-2"
    assert page.client_url == "onenote:///pg-2"
    assert page.content_url.endswith("/pages/pg-2/content")
    assert page.modified_at == datetime(2024, 6, 10, 9, 30, tzinfo=timezone.utc)
    assert page.created_by_app_id == "app-onenote"


def test_subpages_get_their_parent_page_id(fake_graph):
    tree = crawler(fake_graph).crawl()
    subpage = next(p for p in tree.iter_pages() if p.id == "pg-2a")
    assert subpage.level == 1 and subpage.parent_page_id == "pg-2"


def test_notebook_and_section_authorship_is_captured(fake_graph):
    tree = crawler(fake_graph).crawl()
    notebook = tree.notebooks[0]
    assert notebook.owner == "Ada Lovelace"
    assert notebook.last_modified_by == "Grace Hopper"
    assert next(tree.iter_sections()).created_by == "Ada Lovelace"


def test_inaccessible_section_is_recorded_without_aborting_the_crawl(fake_graph):
    instance = crawler(fake_graph)
    tree = instance.crawl()
    restricted = next(s for s in tree.iter_sections() if s.id == "sec-locked")
    assert restricted.error and "inaccessible" in restricted.error
    assert restricted.pages == []
    assert any(e["error"] == "inaccessible" for e in instance.errors)
    # the rest of the hierarchy still came through
    assert tree.counts()["pages"] == 4


def test_notebook_name_filter(fake_graph):
    config = ParserConfig()
    config.filters.notebook_names = ("Engineering",)
    tree = crawler(fake_graph, config).crawl()
    assert [n.display_name for n in tree.notebooks] == ["Engineering"]


def test_notebook_exclusion_filter(fake_graph):
    config = ParserConfig()
    config.filters.exclude_notebook_names = ("Archive 2019",)
    tree = crawler(fake_graph, config).crawl()
    assert [n.display_name for n in tree.notebooks] == ["Engineering"]


def test_regex_filters(fake_graph):
    config = ParserConfig()
    config.filters.use_regex = True
    config.filters.notebook_names = (r"^Archive\s+\d{4}$",)
    tree = crawler(fake_graph, config).crawl()
    assert [n.display_name for n in tree.notebooks] == ["Archive 2019"]


def test_section_filter_skips_other_sections(fake_graph):
    config = ParserConfig()
    config.filters.section_names = ("Commissioning",)
    tree = crawler(fake_graph, config).crawl()
    assert [s.display_name for s in tree.iter_sections()] == ["Commissioning"]


def test_notebook_id_filter_fetches_only_that_notebook(fake_graph):
    config = ParserConfig()
    config.filters.notebook_ids = ("nb-archive",)
    tree = crawler(fake_graph, config).crawl()
    assert [n.id for n in tree.notebooks] == ["nb-archive"]


def test_modified_since_filter_drops_older_pages(fake_graph):
    config = ParserConfig()
    config.filters.modified_since = datetime(2024, 6, 1, tzinfo=timezone.utc)
    tree = crawler(fake_graph, config).crawl()
    assert {p.id for p in tree.iter_pages()} == {"pg-2", "pg-2a"}


def test_include_subpages_false_drops_subpages(fake_graph):
    config = ParserConfig()
    config.filters.include_subpages = False
    tree = crawler(fake_graph, config).crawl()
    assert "pg-2a" not in {p.id for p in tree.iter_pages()}


def test_page_id_filter(fake_graph):
    config = ParserConfig()
    config.filters.page_ids = ("pg-1",)
    tree = crawler(fake_graph, config).crawl()
    assert [p.id for p in tree.iter_pages()] == ["pg-1"]


def test_crawl_survives_a_notebook_listing_failure(fake_graph):
    fake_graph.notebooks = []
    instance = crawler(fake_graph)
    assert instance.crawl().counts()["notebooks"] == 0


@pytest.mark.parametrize(
    "levels,expected",
    [
        ([0, 1, 2, 1, 0], [None, "p0", "p1", "p0", None]),
        ([0, 0, 0], [None, None, None]),
        ([1, 2], [None, "p0"]),  # orphan subpage: no parent above it
    ],
)
def test_parent_assignment_from_level_sequences(levels, expected):
    pages = [PageRef(id=f"p{i}", level=level, order=i) for i, level in enumerate(levels)]
    assign_parent_pages(pages)
    assert [p.parent_page_id for p in pages] == expected


def test_name_matcher_is_case_insensitive_and_exclusion_wins():
    matcher = NameMatcher(["Engineering"], ["engineering"], use_regex=False)
    assert matcher.matches("Engineering") is False
    assert NameMatcher(["Engineering"], [], use_regex=False).matches("ENGINEERING") is True
    assert NameMatcher([], [], use_regex=False).matches("anything") is True


# -- recent notebooks -------------------------------------------------------- #


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://contoso.sharepoint.com/sites/Streetlight/SiteAssets/Notes",
         "contoso.sharepoint.com:/sites/Streetlight"),
        ("https://contoso.sharepoint.com/teams/Eng/SiteAssets/Eng%20Notebook",
         "contoso.sharepoint.com:/teams/Eng"),
        ("https://contoso-my.sharepoint.com/personal/a_b_contoso_com/Documents/N",
         "contoso-my.sharepoint.com:/personal/a_b_contoso_com"),
        # URL-encoded site names decode before the address is built
        ("https://contoso.sharepoint.com/sites/Street%20Light/SiteAssets/N",
         "contoso.sharepoint.com:/sites/Street Light"),
        # A library on the root site collection - no /sites/ segment at all.
        ("https://contoso.sharepoint.com/Tools%20and%20Forms/BC/OneNotes/Streetlighting",
         "contoso.sharepoint.com"),
        ("https://contoso.sharepoint.com/", "contoso.sharepoint.com"),
        ("https://d.docs.live.net/abc/Documents", None),   # consumer OneDrive
        ("https://example.com/whatever", None),
        (None, None),
        ("", None),
    ],
)
def test_site_address_from_web_url(url, expected):
    from onenote_parser.discovery import site_address_from_web_url

    assert site_address_from_web_url(url) == expected


def test_resolve_site_target_returns_the_composite_id(fake_graph):
    """OneNote rejects hostname/colon addresses; only the composite id works."""
    from onenote_parser.discovery import resolve_site_target
    from onenote_parser.graph.transport import Response, _CaseInsensitiveDict

    seen = []

    def lookup(method, url, **kwargs):
        seen.append(url)
        body = json.dumps({"id": "contoso.sharepoint.com,aaaa-bbbb,cccc-dddd"})
        return Response(200, body.encode(),
                        _CaseInsensitiveDict({"Content-Type": "application/json"}), url)

    fake_graph.request = lookup
    client = GraphClient(StubTokenProvider(), ParserConfig().graph, transport=fake_graph,
                         sleep=lambda _s: None)
    target = resolve_site_target(
        client, "https://contoso.sharepoint.com/sites/Streetlight/SiteAssets/N")
    assert target == "sites/contoso.sharepoint.com,aaaa-bbbb,cccc-dddd"
    assert "/sites/contoso.sharepoint.com:/sites/Streetlight" in seen[0]


def test_resolve_site_target_is_none_for_non_sharepoint_urls(fake_graph):
    from onenote_parser.discovery import resolve_site_target

    client = GraphClient(StubTokenProvider(), ParserConfig().graph, transport=fake_graph,
                         sleep=lambda _s: None)
    assert resolve_site_target(client, "https://d.docs.live.net/abc") is None
    assert resolve_site_target(client, None) is None


def test_resolve_site_target_survives_an_unreadable_site(fake_graph):
    from onenote_parser.discovery import resolve_site_target
    from onenote_parser.graph.transport import Response, _CaseInsensitiveDict

    fake_graph.request = lambda m, u, **k: Response(
        403, b'{"error":{"code":"accessDenied"}}',
        _CaseInsensitiveDict({"Content-Type": "application/json"}), u)
    client = GraphClient(StubTokenProvider(), ParserConfig().graph, transport=fake_graph,
                         sleep=lambda _s: None)
    assert resolve_site_target(
        client, "https://contoso.sharepoint.com/sites/X/SiteAssets/N") is None


def test_list_recent_notebooks_parses_and_sorts_by_recency(fake_graph):
    from onenote_parser.discovery import list_recent_notebooks

    def recent(method, url, **kwargs):
        from onenote_parser.graph.transport import Response, _CaseInsensitiveDict

        payload = json.dumps({"value": [
            {"displayName": "Peyman @ Primary", "lastAccessedTime": "2025-08-22T22:12:31Z",
             "sourceService": "OneDrive",
             "links": {"oneNoteWebUrl": {"href":
                       "https://c-my.sharepoint.com/personal/p_c_com/Documents/N"}}},
            {"displayName": "Streetlight", "lastAccessedTime": "2026-08-27T10:00:00Z",
             "sourceService": "SharePoint",
             "links": {"oneNoteWebUrl": {"href":
                       "https://c.sharepoint.com/sites/Streetlight/SiteAssets/N"}}},
        ]})
        return Response(200, payload.encode(),
                        _CaseInsensitiveDict({"Content-Type": "application/json"}), url)

    fake_graph.request = recent
    client = GraphClient(StubTokenProvider(), ParserConfig().graph, transport=fake_graph,
                         sleep=lambda _s: None)
    notebooks = list_recent_notebooks(client)

    assert [n.display_name for n in notebooks] == ["Streetlight", "Peyman @ Primary"]
    assert notebooks[0].site_address == "c.sharepoint.com:/sites/Streetlight"
    assert notebooks[0].to_dict()["source_service"] == "SharePoint"


def test_list_recent_notebooks_handles_an_empty_response(fake_graph):
    from onenote_parser.discovery import list_recent_notebooks
    from onenote_parser.graph.transport import Response, _CaseInsensitiveDict

    fake_graph.request = lambda m, u, **k: Response(
        200, b'{"value":[]}', _CaseInsensitiveDict({"Content-Type": "application/json"}), u)
    client = GraphClient(StubTokenProvider(), ParserConfig().graph, transport=fake_graph,
                         sleep=lambda _s: None)
    assert list_recent_notebooks(client) == []
