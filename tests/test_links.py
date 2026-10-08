"""Hyperlink classification and OneNote deep-link id extraction."""

from __future__ import annotations

import pytest

from onenote_parser.models import LinkKind
from onenote_parser.parsing.links import classify_link, describe_link, extract_onenote_ids

ONENOTE_PROTOCOL = (
    "onenote:https://contoso.sharepoint.com/sites/eng/SiteAssets/Eng.one#Vibration"
    "&section-id={1111}&page-id={2222}&end"
)
ONENOTE_WEB = (
    "https://contoso.onenote.com/view.aspx?resid=ABC&wd=target%28Eng.one%7C1111%2F%29"
)


@pytest.mark.parametrize(
    "href,kind",
    [
        (ONENOTE_PROTOCOL, LinkKind.ONENOTE),
        (ONENOTE_WEB, LinkKind.ONENOTE),
        ("https://www.onenote.com/notebooks", LinkKind.ONENOTE),
        ("https://example.com/spec.pdf?page-id=nope", LinkKind.ONENOTE),
        ("https://standards.example.com/iso-9906", LinkKind.EXTERNAL),
        ("http://intranet/wiki", LinkKind.EXTERNAL),
        ("mailto:eng@contoso.com", LinkKind.MAILTO),
        ("#section-2", LinkKind.ANCHOR),
        ("file:///C:/specs/pump.pdf", LinkKind.FILE),
        ("\\\\fileserver\\specs\\pump.pdf", LinkKind.FILE),
        ("", LinkKind.UNKNOWN),
        ("javascript:void(0)", LinkKind.UNKNOWN),
    ],
)
def test_classify_link(href, kind):
    assert classify_link(href) is kind


def test_extract_ids_from_the_onenote_protocol_form():
    ids = extract_onenote_ids(ONENOTE_PROTOCOL)
    assert ids == {"section_id": "{1111}", "page_id": "{2222}"}


def test_extract_ids_from_the_web_form():
    href = "https://contoso.onenote.com/x?notebook-id={NB}&section-group-id={SG}"
    assert extract_onenote_ids(href) == {"notebook_id": "{NB}", "section_group_id": "{SG}"}


def test_extract_ids_url_decodes_values():
    assert extract_onenote_ids("onenote:x#&page-id=%7BABC%7D&end")["page_id"] == "{ABC}"


def test_extract_ids_returns_empty_for_plain_links():
    assert extract_onenote_ids("https://example.com") == {}
    assert extract_onenote_ids("") == {}


def test_describe_link_payload_shape():
    external = describe_link("https://example.com", title="Spec")
    assert external == {"href": "https://example.com", "kind": "external", "title": "Spec"}

    internal = describe_link(ONENOTE_PROTOCOL)
    assert internal["kind"] == "onenote"
    assert internal["page_id"] == "{2222}"
