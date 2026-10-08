"""Hyperlink classification.

OneNote pages mix ordinary web links with *internal* links that point at another
notebook, section or page.  Downstream systems need those separated: an internal
link is a graph edge inside the corpus, an external one is not.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from ..models import LinkKind

__all__ = ["classify_link", "extract_onenote_ids", "describe_link"]

_ONENOTE_HOSTS = (
    "onenote.com",
    "onenote.officeapps.live.com",
    "onenote.osi.office.net",
)

#: ``page-id={GUID}{N}{XXXX}`` style identifiers found in OneNote deep links.
_ID_PATTERNS = {
    "page_id": re.compile(r"page-id=([^&#\s]+)", re.IGNORECASE),
    "section_id": re.compile(r"section-id=([^&#\s]+)", re.IGNORECASE),
    "notebook_id": re.compile(r"notebook-id=([^&#\s]+)", re.IGNORECASE),
    "section_group_id": re.compile(r"section-group-id=([^&#\s]+)", re.IGNORECASE),
}


def classify_link(href: str) -> LinkKind:
    """Classify a raw ``href`` into a :class:`~onenote_parser.models.LinkKind`."""
    if not href:
        return LinkKind.UNKNOWN
    value = href.strip()
    lowered = value.lower()
    if lowered.startswith("onenote:"):
        return LinkKind.ONENOTE
    if lowered.startswith("mailto:"):
        return LinkKind.MAILTO
    if value.startswith("#"):
        return LinkKind.ANCHOR
    if lowered.startswith(("file:", "\\\\")):
        return LinkKind.FILE
    if lowered.startswith(("http://", "https://")):
        host = (urlparse(value).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in _ONENOTE_HOSTS):
            return LinkKind.ONENOTE
        if any(key in lowered for key in ("page-id=", "section-id=", "notebook-id=")):
            return LinkKind.ONENOTE
        return LinkKind.EXTERNAL
    return LinkKind.UNKNOWN


def extract_onenote_ids(href: str) -> dict[str, str]:
    """Pull ``page-id`` / ``section-id`` / ``notebook-id`` out of a OneNote link.

    Works for both the ``onenote:`` protocol form and the ``https://…onenote.com``
    web form; values are URL-decoded but otherwise left verbatim so they can be
    compared against ids stored elsewhere in the export.
    """
    if not href:
        return {}
    found: dict[str, str] = {}
    for name, pattern in _ID_PATTERNS.items():
        match = pattern.search(href)
        if match:
            found[name] = unquote(match.group(1))
    if not found and href.lower().startswith("onenote:"):
        query = urlparse(href).query
        for key, values in parse_qs(query).items():
            normalized = key.lower().replace("-", "_")
            if normalized in _ID_PATTERNS and values:
                found[normalized] = values[0]
    return found


def describe_link(href: str, *, title: Optional[str] = None) -> dict[str, Any]:
    """Build the attribute payload stored on a ``link`` mark."""
    kind = classify_link(href)
    payload: dict[str, Any] = {"href": href, "kind": kind.value}
    if title:
        payload["title"] = title
    if kind is LinkKind.ONENOTE:
        payload.update(extract_onenote_ids(href))
    return payload
