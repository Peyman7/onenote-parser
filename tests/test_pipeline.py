"""End-to-end pipeline behaviour against the fake Graph service."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onenote_parser.config import ParserConfig
from onenote_parser.graph.client import GraphClient
from onenote_parser.models import ChangeStatus, FileBlock, ImageBlock, PageRef, TableBlock
from onenote_parser.pipeline import OneNoteParser
from onenote_parser.resources import CallableOcrEngine

from conftest import StubTokenProvider


def build(config: ParserConfig, fake_graph, **kwargs) -> OneNoteParser:
    client = GraphClient(StubTokenProvider(), config.graph, transport=fake_graph,
                         sleep=lambda _s: None)
    return OneNoteParser(config, client=client, **kwargs)


@pytest.fixture
def parser(base_config, fake_graph):
    with build(base_config, fake_graph) as instance:
        yield instance


# -- full run ---------------------------------------------------------------- #


def test_first_run_parses_everything_as_new(parser, base_config):
    result = parser.run()
    assert result.stats.pages_discovered == 4
    assert result.stats.pages_parsed == 4
    assert result.changes.counts()["new"] == 4
    assert result.stats.pages_failed == 0
    assert {p.id for p in result.pages} == {"pg-1", "pg-2", "pg-2a", "pg-3"}


def test_pages_carry_full_hierarchy_context(parser):
    page = parser.run().page_by_id("pg-2")
    assert page.path == ["Engineering", "Pumps", "Centrifugal", "Commissioning"]
    assert page.breadcrumb == \
        "Engineering > Pumps > Centrifugal > Commissioning > Pump Commissioning"
    assert page.notebook_id == "nb-eng" and page.section_id == "sec-commissioning"
    assert page.metadata.web_url == "https://onenote.com/pg-2"
    assert page.metadata.created_by_app_id == "app-onenote"
    assert page.content_hash


def test_subpage_metadata_records_its_parent(parser):
    page = parser.run().page_by_id("pg-2a")
    assert page.metadata.level == 1 and page.metadata.parent_page_id == "pg-2"


def test_content_is_normalized_into_blocks(parser):
    page = parser.run().page_by_id("pg-2")
    types = [b.type.value for b in page.blocks]
    assert types[:4] == ["paragraph", "list", "table", "image"]
    assert page.flags.has_tables and page.flags.has_images and page.flags.has_attachments
    assert "Follow the spec." in page.text
    table = next(b for b in page.blocks if isinstance(b, TableBlock))
    assert table.has_header


def test_resources_are_downloaded_and_attachment_text_extracted(parser, base_config):
    result = parser.run()
    page = result.page_by_id("pg-2")
    image = next(b for b in page.blocks if isinstance(b, ImageBlock))
    attachment = next(b for b in page.blocks if isinstance(b, FileBlock))

    assert image.downloaded and image.mime_type == "image/png"
    assert (Path(base_config.export.output_dir) / image.file_path).exists()
    assert attachment.extraction_status == "ok"
    assert "Commissioning report" in attachment.extracted_text
    assert result.stats.images_downloaded == 1
    assert result.stats.attachments_downloaded == 1


def test_inaccessible_section_is_reported_but_does_not_fail_the_run(parser):
    result = parser.run()
    assert any(e["error"] == "inaccessible" for e in result.errors)
    assert result.stats.pages_parsed == 4


def test_page_that_returns_403_is_recorded_as_inaccessible(base_config, fake_graph):
    fake_graph.forbidden_pages.add("pg-1")
    with build(base_config, fake_graph) as parser:
        result = parser.run()
    assert result.stats.pages_failed == 1
    assert [c.page_id for c in result.changes.by_status(ChangeStatus.INACCESSIBLE)] == ["pg-1"]
    assert result.page_by_id("pg-1") is None
    assert result.stats.pages_parsed == 3


def test_page_that_disappears_mid_run_is_recorded_not_fatal(base_config, fake_graph):
    parser = build(base_config, fake_graph)
    original = parser.fetch_page_html

    def vanish(page):
        if getattr(page, "id", page) == "pg-3":
            fake_graph.delete_page("pg-3")
        return original(page)

    parser.fetch_page_html = vanish
    result = parser.run()
    parser.close()
    assert result.stats.pages_failed == 1
    assert any(e["kind"] == "not_found" for e in result.errors)


# -- incremental sync -------------------------------------------------------- #


def test_second_run_skips_unchanged_pages_without_fetching_content(base_config, fake_graph):
    with build(base_config, fake_graph) as parser:
        parser.run()

    fake_graph.requests.clear()
    with build(base_config, fake_graph) as parser:
        result = parser.run()

    assert result.changes.counts()["unchanged"] == 4
    assert result.stats.pages_skipped == 4
    assert not [url for _m, url in fake_graph.requests if "/content" in url]


def test_edited_page_is_refetched_and_reported_modified(base_config, fake_graph):
    with build(base_config, fake_graph) as parser:
        parser.run()

    fake_graph.edit_page("pg-1", "<p data-id='p1'>Rewritten handbook index.</p>",
                         "2024-07-01T00:00:00Z")
    with build(base_config, fake_graph) as parser:
        result = parser.run()

    modified = result.changes.by_status(ChangeStatus.MODIFIED)
    assert [c.page_id for c in modified] == ["pg-1"]
    assert modified[0].previous_hash != modified[0].current_hash
    assert "Rewritten" in result.page_by_id("pg-1").text


def test_deleted_page_is_detected_on_the_next_run(base_config, fake_graph):
    with build(base_config, fake_graph) as parser:
        parser.run()

    fake_graph.delete_page("pg-3")
    with build(base_config, fake_graph) as parser:
        result = parser.run()

    assert [c.page_id for c in result.changes.by_status(ChangeStatus.DELETED)] == ["pg-3"]


def test_deletion_detection_is_suppressed_for_filtered_runs(base_config, fake_graph):
    with build(base_config, fake_graph) as parser:
        parser.run()

    base_config.filters.notebook_names = ("Engineering",)
    with build(base_config, fake_graph) as parser:
        result = parser.run()

    # pg-3 lives in the Archive notebook, which this run never visited.
    assert result.changes.by_status(ChangeStatus.DELETED) == []


def test_full_mode_reparses_everything_but_still_reports_unchanged(base_config, fake_graph):
    with build(base_config, fake_graph) as parser:
        parser.run()

    base_config.state.mode = "full"
    fake_graph.requests.clear()
    with build(base_config, fake_graph) as parser:
        result = parser.run()

    assert len([u for _m, u in fake_graph.requests if "/content" in u]) == 4
    assert result.changes.counts()["unchanged"] == 4
    assert result.stats.pages_parsed == 4


def test_state_disabled_treats_every_run_as_first(base_config, fake_graph):
    base_config.state.enabled = False
    with build(base_config, fake_graph) as parser:
        parser.run()
    with build(base_config, fake_graph) as parser:
        result = parser.run()
    assert result.changes.counts()["new"] == 4


# -- filters, limits, dry run ------------------------------------------------ #


def test_max_pages_limits_the_run(base_config, fake_graph):
    base_config.filters.max_pages = 2
    with build(base_config, fake_graph) as parser:
        result = parser.run()
    assert result.stats.pages_parsed == 2 and result.stats.pages_discovered == 4


def test_dry_run_discovers_without_fetching_content(base_config, fake_graph):
    base_config.runtime.dry_run = True
    with build(base_config, fake_graph) as parser:
        result = parser.run()
    assert result.stats.pages_discovered == 4 and result.pages == []
    assert not [u for _m, u in fake_graph.requests if "/content" in u]
    assert not Path(base_config.state.path).exists()


def test_resource_download_can_be_disabled(base_config, fake_graph):
    base_config.images.download = False
    base_config.attachments.download = False
    base_config.attachments.extract_text = False
    with build(base_config, fake_graph) as parser:
        result = parser.run()
    image = next(b for b in result.page_by_id("pg-2").blocks if isinstance(b, ImageBlock))
    assert image.downloaded is False and image.file_path is None
    assert result.stats.images_downloaded == 0


# -- concurrency ------------------------------------------------------------- #


@pytest.mark.parametrize("concurrency", [1, 4])
def test_output_order_is_deterministic_regardless_of_concurrency(base_config, fake_graph,
                                                                 concurrency):
    base_config.runtime.concurrency = concurrency
    with build(base_config, fake_graph) as parser:
        result = parser.run()
    assert [p.id for p in result.pages] == ["pg-1", "pg-2", "pg-2a", "pg-3"]


def test_concurrent_and_serial_runs_produce_identical_output(base_config, fake_graph, tmp_path):
    base_config.runtime.concurrency = 1
    with build(base_config, fake_graph) as parser:
        serial = [p.to_dict() for p in parser.run(export=False).pages]

    config = base_config.copy()
    config.state.path = str(tmp_path / "state2.json")
    config.runtime.concurrency = 4
    with build(config, fake_graph) as parser:
        parallel = [p.to_dict() for p in parser.run(export=False).pages]

    for a, b in zip(serial, parallel):
        a["metadata"].pop("fetched_at")
        b["metadata"].pop("fetched_at")
    assert serial == parallel


# -- throttling -------------------------------------------------------------- #


def test_throttled_requests_are_retried_transparently(base_config, fake_graph):
    url = "https://graph.microsoft.com/v1.0/me/onenote/pages/pg-1/content"
    fake_graph.fail_once[url] = 1
    base_config.graph.default_retry_after_seconds = 0.001
    with build(base_config, fake_graph) as parser:
        result = parser.run()
    assert result.stats.graph_throttled >= 1
    assert result.page_by_id("pg-1") is not None


# -- single page API --------------------------------------------------------- #


def test_parse_page_works_standalone(parser):
    page = parser.parse_page(PageRef(id="pg-1", title="Welcome"))
    assert page.title == "Welcome"
    assert "handbook" in page.text
    assert page.content_hash


def test_parse_page_html_needs_no_network(parser):
    html = "<html><head><title>Offline</title></head><body><p>cached</p></body></html>"
    page = parser.parse_page_html(PageRef(id="x", path=["NB", "Sec"]), html)
    assert page.title == "Offline" and page.text == "cached"
    assert page.path == ["NB", "Sec"]


def test_include_ids_is_requested_by_default(parser, fake_graph):
    parser.fetch_page_html(PageRef(id="pg-1"))
    assert any("pg-1/content" in url for _m, url in fake_graph.requests)


def test_multipart_ink_response_is_split_and_stored(base_config, fake_graph):
    boundary = "----boundary"
    html = "<html><body><p>ink page</p></body></html>"
    body = (
        f"--{boundary}\r\nContent-Type: text/html\r\n\r\n{html}\r\n"
        f"--{boundary}\r\nContent-Type: application/inkml+xml\r\n\r\n<ink/>\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    parsed_html, parts = OneNoteParser._split_multipart(
        body, f'multipart/form-data; boundary="{boundary}"'
    )
    assert "ink page" in parsed_html
    assert parts == [("application/inkml+xml", b"<ink/>")]

    with build(base_config, fake_graph) as parser:
        page = parser.parse_page_html(PageRef(id="pg-ink"), parsed_html, ink_parts=parts)
    ink_block = next(b for b in page.blocks if isinstance(b, FileBlock))
    assert ink_block.mime_type == "application/inkml+xml" and ink_block.downloaded


# -- exports ----------------------------------------------------------------- #


def test_run_writes_all_configured_formats(base_config, fake_graph):
    base_config.export.formats = ("json", "jsonl", "markdown")
    with build(base_config, fake_graph) as parser:
        parser.run()
    output = Path(base_config.export.output_dir)
    assert (output / "index.json").exists()
    assert (output / "pages.jsonl").exists()
    assert (output / "markdown" / "README.md").exists()
    assert len(json.loads((output / "index.json").read_text())["pages"]) == 4


def test_export_can_be_deferred(base_config, fake_graph):
    with build(base_config, fake_graph) as parser:
        result = parser.run(export=False)
        assert not Path(base_config.export.output_dir).joinpath("index.json").exists()
        parser.export(result)
    assert Path(base_config.export.output_dir).joinpath("index.json").exists()


# -- OCR integration --------------------------------------------------------- #


def test_ocr_text_reaches_the_page_text_and_flags(base_config, fake_graph):
    base_config.ocr.enabled = True
    base_config.ocr.min_width = 1
    base_config.ocr.min_height = 1
    engine = CallableOcrEngine(lambda data: "PUMP CURVE 4.5", name="stub")
    with build(base_config, fake_graph, ocr_engine=engine) as parser:
        result = parser.run()
    page = result.page_by_id("pg-2")
    assert page.flags.ocr_applied
    assert "PUMP CURVE 4.5" in page.text
    assert result.stats.images_ocr == 1


# -- errors ------------------------------------------------------------------ #


def test_errors_are_collected_and_the_run_still_completes(base_config, fake_graph):
    fake_graph.forbidden_pages.update({"pg-1", "pg-3"})
    with build(base_config, fake_graph) as parser:
        result = parser.run()
    assert result.stats.pages_failed == 2
    assert result.stats.pages_parsed == 2
    manifest = json.loads((Path(base_config.export.output_dir) / "manifest.json").read_text())
    assert len(manifest["errors"]) >= 2


def test_continue_on_error_false_propagates(base_config, fake_graph):
    from onenote_parser.errors import GraphHTTPError

    base_config.runtime.continue_on_error = False
    base_config.runtime.concurrency = 1

    class Boom(dict):
        pass

    with build(base_config, fake_graph) as parser:
        def explode(page):
            raise GraphHTTPError("boom", status_code=500, url="x")

        parser.fetch_page_html = explode
        with pytest.raises(GraphHTTPError):
            parser.run()
