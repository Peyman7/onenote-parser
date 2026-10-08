"""Shared fixtures.

The centrepiece is :class:`FakeGraph`, an in-memory stand-in for the Microsoft
Graph OneNote API.  It serves a notebook with nested section groups, subpages,
an inaccessible section, a deleted page and binary resources, so the whole
pipeline can be exercised deterministically without network access.
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
import io
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from onenote_parser.config import ParserConfig  # noqa: E402
from onenote_parser.graph.transport import Response, _CaseInsensitiveDict  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
) + b"\x00" * 2048

PDF_BYTES = b"%PDF-1.4 fake pdf payload for tests" + b"\x00" * 2048


def make_docx(paragraphs: list[str]) -> bytes:
    """Minimal but real OOXML .docx package."""
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def page_html(title: str, body: str) -> str:
    return (
        f'<html lang="en-US"><head><title>{title}</title>'
        f'<meta name="created" content="2024-01-05T08:00:00.0000000"/></head>'
        f'<body data-absolute-enabled="true">'
        f'<div data-id="title"><h1>{title}</h1></div>'
        f'<div data-id="_default" style="position:absolute;left:48px;top:120px">{body}</div>'
        f"</body></html>"
    )


RESOURCE_BASE = "https://graph.microsoft.com/v1.0/me/onenote/resources"


class FakeGraph:
    """Routes Graph URLs to canned payloads; records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.fail_once: dict[str, int] = {}
        self.pages: dict[str, dict[str, Any]] = {}
        self.page_bodies: dict[str, str] = {}
        self.deleted_pages: set[str] = set()
        self.forbidden_sections: set[str] = {"sec-locked"}
        self.forbidden_pages: set[str] = set()
        self._build()

    # -- data ------------------------------------------------------------- #

    def _build(self) -> None:
        self.notebooks = [
            {
                "id": "nb-eng",
                "displayName": "Engineering",
                "createdDateTime": "2023-01-01T00:00:00Z",
                "lastModifiedDateTime": "2024-06-01T00:00:00Z",
                "isDefault": True,
                "isShared": True,
                "createdBy": {"user": {"displayName": "Ada Lovelace", "id": "u1"}},
                "lastModifiedBy": {"user": {"displayName": "Grace Hopper", "id": "u2"}},
                "links": {
                    "oneNoteWebUrl": {"href": "https://onenote.com/nb-eng"},
                    "oneNoteClientUrl": {"href": "onenote:///nb-eng"},
                },
            },
            {
                "id": "nb-archive",
                "displayName": "Archive 2019",
                "createdDateTime": "2019-01-01T00:00:00Z",
                "lastModifiedDateTime": "2019-12-01T00:00:00Z",
                "links": {"oneNoteWebUrl": {"href": "https://onenote.com/nb-archive"}},
            },
        ]
        self.notebook_sections = {
            "nb-eng": [self._section("sec-root", "General")],
            "nb-archive": [self._section("sec-old", "Old Notes")],
        }
        self.notebook_groups = {
            "nb-eng": [self._group("grp-pumps", "Pumps")],
            "nb-archive": [],
        }
        self.group_groups = {"grp-pumps": [self._group("grp-centrifugal", "Centrifugal")]}
        self.group_sections = {
            "grp-pumps": [self._section("sec-locked", "Restricted")],
            "grp-centrifugal": [self._section("sec-commissioning", "Commissioning")],
        }

        self._add_page("pg-1", "sec-root", "Welcome", level=0, order=0,
                       modified="2024-05-01T10:00:00Z",
                       body="<p data-id='p1'>Team <b>handbook</b> index.</p>")
        self._add_page("pg-2", "sec-commissioning", "Pump Commissioning", level=0, order=0,
                       modified="2024-06-10T09:30:00Z",
                       body=(
                           "<p data-id='p2'>Follow <a href='https://ex.com/spec'>the spec</a>.</p>"
                           "<ul><li>Check alignment</li><li>Check lube</li></ul>"
                           "<table><tr><td><p><b>Item</b></p></td><td><p><b>Limit</b></p></td></tr>"
                           "<tr><td><p>Vibration</p></td><td><p>4.5 mm/s</p></td></tr></table>"
                           f"<img data-id='img1' src='{RESOURCE_BASE}/res-img1/$value' "
                           "data-src-type='image/png' alt='pump curve' width='320' height='240'/>"
                           f"<object data-id='obj1' data-attachment='report.docx' "
                           f"data='{RESOURCE_BASE}/res-doc1/$value' "
                           "type='application/vnd.openxmlformats-officedocument."
                           "wordprocessingml.document'/>"
                       ))
        self._add_page("pg-2a", "sec-commissioning", "Vibration Limits", level=1, order=1,
                       modified="2024-06-11T09:30:00Z",
                       body="<p data-id='p3'>Alarm at 7.1 mm/s.</p>")
        self._add_page("pg-3", "sec-old", "Legacy Procedure", level=0, order=0,
                       modified="2019-03-03T12:00:00Z",
                       body="<p data-id='p4'>Superseded.</p>")

        self.resources = {
            "res-img1": (PNG_BYTES, "image/png"),
            "res-doc1": (make_docx(["Commissioning report", "All checks passed."]),
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        }

    @staticmethod
    def _section(section_id: str, name: str) -> dict[str, Any]:
        return {
            "id": section_id,
            "displayName": name,
            "createdDateTime": "2023-02-01T00:00:00Z",
            "lastModifiedDateTime": "2024-06-01T00:00:00Z",
            "isDefault": False,
            "createdBy": {"user": {"displayName": "Ada Lovelace", "id": "u1"}},
            "lastModifiedBy": {"user": {"displayName": "Grace Hopper", "id": "u2"}},
            "links": {"oneNoteWebUrl": {"href": f"https://onenote.com/{section_id}"}},
        }

    @staticmethod
    def _group(group_id: str, name: str) -> dict[str, Any]:
        return {
            "id": group_id,
            "displayName": name,
            "createdDateTime": "2023-02-01T00:00:00Z",
            "lastModifiedDateTime": "2024-06-01T00:00:00Z",
        }

    def _add_page(self, page_id: str, section_id: str, title: str, *, level: int, order: int,
                  modified: str, body: str) -> None:
        self.pages[page_id] = {
            "id": page_id,
            "title": title,
            "level": level,
            "order": order,
            "createdDateTime": "2024-01-05T08:00:00Z",
            "lastModifiedDateTime": modified,
            "createdByAppId": "app-onenote",
            "contentUrl": f"https://graph.microsoft.com/v1.0/me/onenote/pages/{page_id}/content",
            "links": {
                "oneNoteWebUrl": {"href": f"https://onenote.com/{page_id}"},
                "oneNoteClientUrl": {"href": f"onenote:///{page_id}"},
            },
            "_section": section_id,
        }
        self.page_bodies[page_id] = page_html(title, body)

    # -- mutation helpers used by tests ----------------------------------- #

    def edit_page(self, page_id: str, body: str, modified: str) -> None:
        self.page_bodies[page_id] = page_html(self.pages[page_id]["title"], body)
        self.pages[page_id]["lastModifiedDateTime"] = modified

    def delete_page(self, page_id: str) -> None:
        self.deleted_pages.add(page_id)

    # -- routing ---------------------------------------------------------- #

    def request(self, method: str, url: str, *, headers=None, params=None, timeout=None
                ) -> Response:
        self.requests.append((method, url))
        remaining = self.fail_once.get(url)
        if remaining:
            self.fail_once[url] = remaining - 1
            return Response(429, b'{"error":{"code":"TooManyRequests"}}',
                            _CaseInsensitiveDict({"Content-Type": "application/json"}), url)

        path = urlparse(url).path
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if params:
            query.update({k: str(v) for k, v in params.items()})

        match = re.search(r"/resources/([^/]+)/\$value", path)
        if match:
            resource_id = match.group(1)
            if resource_id not in self.resources:
                return self._error(404, "ResourceNotFound", url)
            data, mime = self.resources[resource_id]
            return Response(200, data, _CaseInsensitiveDict({"Content-Type": mime}), url)

        match = re.search(r"/pages/([^/]+)/content", path)
        if match:
            page_id = match.group(1)
            if page_id in self.forbidden_pages:
                return self._error(403, "AccessDenied", url)
            if page_id in self.deleted_pages or page_id not in self.page_bodies:
                return self._error(404, "ItemNotFound", url)
            return Response(
                200, self.page_bodies[page_id].encode("utf-8"),
                _CaseInsensitiveDict({"Content-Type": "text/html; charset=utf-8"}), url,
            )

        match = re.search(r"/sections/([^/]+)/pages", path)
        if match:
            section_id = match.group(1)
            if section_id in self.forbidden_sections:
                return self._error(403, "AccessDenied", url)
            pages = [
                {k: v for k, v in page.items() if not k.startswith("_")}
                for page in self.pages.values()
                if page["_section"] == section_id and page["id"] not in self.deleted_pages
            ]
            return self._collection(pages, url)

        match = re.search(r"/sectionGroups/([^/]+)/sectionGroups", path)
        if match:
            return self._collection(self.group_groups.get(match.group(1), []), url)
        match = re.search(r"/sectionGroups/([^/]+)/sections", path)
        if match:
            return self._collection(self.group_sections.get(match.group(1), []), url)
        match = re.search(r"/notebooks/([^/]+)/sectionGroups", path)
        if match:
            return self._collection(self.notebook_groups.get(match.group(1), []), url)
        match = re.search(r"/notebooks/([^/]+)/sections", path)
        if match:
            return self._collection(self.notebook_sections.get(match.group(1), []), url)
        if path.endswith("/onenote/notebooks"):
            return self._collection(self.notebooks, url)
        match = re.search(r"/notebooks/([^/]+)$", path)
        if match:
            for notebook in self.notebooks:
                if notebook["id"] == match.group(1):
                    return self._json(notebook, url)
            return self._error(404, "ItemNotFound", url)

        return self._error(404, "UnroutedTestUrl:" + path, url)

    def close(self) -> None:
        pass

    @staticmethod
    def _json(payload: Mapping[str, Any], url: str) -> Response:
        return Response(200, json.dumps(payload).encode(),
                        _CaseInsensitiveDict({"Content-Type": "application/json"}), url)

    def _collection(self, values: list[dict[str, Any]], url: str) -> Response:
        return self._json({"value": values}, url)

    @staticmethod
    def _error(status: int, code: str, url: str) -> Response:
        payload = json.dumps({"error": {"code": code, "message": code}})
        return Response(status, payload.encode(),
                        _CaseInsensitiveDict({"Content-Type": "application/json",
                                              "request-id": "req-test"}), url)


class StubTokenProvider:
    def __init__(self, token: str = "test-token") -> None:
        self.token = token
        self.calls = 0
        self.invalidated = 0

    def get_token(self) -> str:
        self.calls += 1
        return self.token

    def invalidate(self) -> None:
        self.invalidated += 1
        self.token = "refreshed-token"


@pytest.fixture
def fake_graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def token_provider() -> StubTokenProvider:
    return StubTokenProvider()


@pytest.fixture
def base_config(tmp_path: Path) -> ParserConfig:
    config = ParserConfig()
    config.auth.mode = "static"
    config.auth.access_token = "test-token"
    config.export.output_dir = str(tmp_path / "output")
    config.state.path = str(tmp_path / "state" / "state.json")
    config.ocr.cache_path = str(tmp_path / "output" / "ocr-cache.json")
    config.runtime.concurrency = 1
    config.runtime.log_level = "CRITICAL"
    return config


@pytest.fixture
def rich_html() -> str:
    return (FIXTURES / "page_rich.html").read_text(encoding="utf-8")
