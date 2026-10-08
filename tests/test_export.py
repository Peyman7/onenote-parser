"""Export formats and on-disk layout."""

from __future__ import annotations

import json

import pytest

from onenote_parser.config import ExportConfig
from onenote_parser.errors import ExportError
from onenote_parser.export import (
    JsonExporter,
    JsonlExporter,
    MarkdownExporter,
    create_exporters,
    slugify,
)
from onenote_parser.export.base import page_key
from onenote_parser.models import (
    ChangeSet,
    ChangeStatus,
    HeadingBlock,
    ImageBlock,
    NotebookNode,
    NotebookTree,
    PageChange,
    PageMetadata,
    PageRef,
    ParagraphBlock,
    ParsedPage,
    ParseResult,
    RichText,
    RunStats,
    SectionNode,
    utcnow,
)


@pytest.fixture
def result() -> ParseResult:
    parent = ParsedPage(
        id="pg-1", title="Pump Commissioning",
        notebook_name="Engineering", section_name="Commissioning",
        path=["Engineering", "Pumps", "Commissioning"],
        blocks=[HeadingBlock(level=2, content=RichText("Scope")),
                ParagraphBlock(content=RichText("Body text"), order=1)],
        text="Scope\n\nBody text", content_hash="hash-1",
        metadata=PageMetadata(level=0, web_url="https://onenote.com/pg-1",
                              modified_at=utcnow()),
    )
    child = ParsedPage(
        id="pg-1a", title="Vibration Limits",
        notebook_name="Engineering", section_name="Commissioning",
        path=["Engineering", "Pumps", "Commissioning"],
        blocks=[ParagraphBlock(content=RichText("Alarm at 7.1 mm/s"))],
        text="Alarm at 7.1 mm/s", content_hash="hash-2",
        metadata=PageMetadata(level=1, parent_page_id="pg-1"),
    )
    tree = NotebookTree(notebooks=[
        NotebookNode(id="nb", display_name="Engineering",
                     sections=[SectionNode(id="sec", display_name="Commissioning",
                                           pages=[PageRef("pg-1"), PageRef("pg-1a")])])
    ])
    changes = ChangeSet()
    changes.add(PageChange(page_id="pg-1", status=ChangeStatus.NEW, title="Pump Commissioning"))
    changes.add(PageChange(page_id="pg-1a", status=ChangeStatus.MODIFIED))
    stats = RunStats(started_at=utcnow(), finished_at=utcnow(), pages_parsed=2)
    return ParseResult(tree=tree, pages=[parent, child], changes=changes, stats=stats,
                       run_id="run-abc")


# -- JSON -------------------------------------------------------------------- #


def test_json_export_layout(tmp_path, result):
    manifest = JsonExporter(ExportConfig()).export(result, tmp_path)
    assert (tmp_path / "index.json").exists()
    assert (tmp_path / "manifest.json").exists()
    assert manifest.page_count == 2
    assert len(list((tmp_path / "pages").glob("*.json"))) == 2


def test_index_lists_every_page_with_a_resolvable_file(tmp_path, result):
    JsonExporter().export(result, tmp_path)
    index = json.loads((tmp_path / "index.json").read_text())
    assert index["run_id"] == "run-abc"
    assert index["counts"]["pages"] == 2
    for entry in index["pages"]:
        assert (tmp_path / entry["file"]).exists()
        assert entry["breadcrumb"].startswith("Engineering > Pumps")


def test_page_json_round_trips_through_the_model(tmp_path, result):
    JsonExporter().export(result, tmp_path)
    path = next((tmp_path / "pages").glob("Pump-Commissioning--*.json"))
    restored = ParsedPage.from_dict(json.loads(path.read_text()))
    assert restored.to_dict() == result.pages[0].to_dict()


def test_manifest_records_stats_changes_and_errors(tmp_path, result):
    result.errors.append({"stage": "page", "kind": "inaccessible", "id": "pg-9"})
    JsonExporter().export(result, tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["changes"]["counts"]["new"] == 1
    assert manifest["changes"]["counts"]["modified"] == 1
    assert manifest["stats"]["pages_parsed"] == 2
    assert manifest["errors"][0]["kind"] == "inaccessible"


def test_single_document_mode(tmp_path, result):
    JsonExporter(ExportConfig(per_page_files=False)).export(result, tmp_path)
    assert not (tmp_path / "pages").exists()
    assert len(json.loads((tmp_path / "pages.json").read_text())["pages"]) == 2


def test_compact_output_omits_indentation(tmp_path, result):
    JsonExporter(ExportConfig(pretty=False)).export(result, tmp_path)
    assert "\n  " not in (tmp_path / "index.json").read_text()


# -- JSONL ------------------------------------------------------------------- #


def test_jsonl_export_is_one_page_per_line(tmp_path, result):
    JsonlExporter().export(result, tmp_path)
    lines = (tmp_path / "pages.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["id"] == "pg-1" and first["schema_version"] and first["breadcrumb"]
    change_lines = (tmp_path / "changes.jsonl").read_text().strip().split("\n")
    changes = [json.loads(line) for line in change_lines]
    assert {c["status"] for c in changes} == {"new", "modified"}


# -- Markdown ---------------------------------------------------------------- #


def test_markdown_mirrors_the_hierarchy_and_nests_subpages(tmp_path, result):
    MarkdownExporter().export(result, tmp_path)
    root = tmp_path / "markdown"
    parent = root / "Engineering" / "Pumps" / "Commissioning"
    parent_file = next(parent.glob("Pump-Commissioning--*.md"))
    assert parent_file.exists()
    child = next(parent_file.with_suffix("").glob("Vibration-Limits--*.md"))
    assert child.exists()
    assert (root / "README.md").exists()


def test_markdown_front_matter_carries_provenance(tmp_path, result):
    MarkdownExporter().export(result, tmp_path)
    text = next((tmp_path / "markdown").rglob("Pump-Commissioning--*.md")).read_text()
    assert text.startswith("---\n")
    assert 'id: "pg-1"' in text
    assert 'content_hash: "hash-1"' in text
    assert 'web_url: "https://onenote.com/pg-1"' in text
    assert "## Scope" in text and "Body text" in text


def test_markdown_front_matter_can_be_disabled(tmp_path, result):
    MarkdownExporter(ExportConfig(markdown_front_matter=False)).export(result, tmp_path)
    text = next((tmp_path / "markdown").rglob("Pump-Commissioning--*.md")).read_text()
    assert text.startswith("# Pump Commissioning")


def test_markdown_flat_layout(tmp_path, result):
    MarkdownExporter(ExportConfig(markdown_mirror_hierarchy=False)).export(result, tmp_path)
    files = sorted(p.name for p in (tmp_path / "markdown").glob("*.md"))
    assert len(files) == 3  # two pages + README


def test_markdown_toc_lists_pages_and_changes(tmp_path, result):
    MarkdownExporter().export(result, tmp_path)
    readme = (tmp_path / "markdown" / "README.md").read_text()
    assert "Pump Commissioning" in readme and "Vibration Limits" in readme
    assert "Changes since last run" in readme and "- new: 1" in readme


# -- shared ------------------------------------------------------------------ #


def test_filenames_are_readable_and_collision_free():
    assert page_key("1-abc!123") != page_key("1-abc123")
    assert slugify("Wärmetauscher / Auslegung 2024") == "Wärmetauscher-Auslegung-2024"
    assert slugify("") == "untitled"
    assert len(slugify("x" * 500)) <= 80


def test_create_exporters_rejects_unknown_formats():
    with pytest.raises(ExportError):
        create_exporters(ExportConfig(formats=("parquet",)))
    assert [e.name for e in create_exporters(ExportConfig(formats=("json", "markdown")))] == \
        ["json", "markdown"]


def test_export_error_names_the_offending_path(tmp_path, result):
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a directory")
    with pytest.raises(ExportError) as excinfo:
        JsonExporter().export(result, blocked)
    assert "blocked" in str(excinfo.value)


def test_image_paths_are_referenced_in_markdown(tmp_path, result):
    result.pages[0].blocks.append(
        ImageBlock(order=2, alt_text="curve", file_path="resources/images/abc-curve.png")
    )
    MarkdownExporter().export(result, tmp_path)
    text = next((tmp_path / "markdown").rglob("Pump-Commissioning--*.md")).read_text()
    assert "![curve](resources/images/abc-curve.png)" in text


# -- delta exports and deleted-page cleanup ---------------------------------- #


def test_index_labels_a_complete_export(tmp_path, result):
    result.stats.pages_discovered = 2
    JsonExporter().export(result, tmp_path)
    index = json.loads((tmp_path / "index.json").read_text())
    assert index["export_scope"] == "all_pages"
    assert index["pages_in_export"] == 2


def test_index_labels_an_incremental_delta_export(tmp_path, result):
    """An incremental run exports only changed pages; say so explicitly."""
    result.stats.pages_discovered = 137
    JsonExporter().export(result, tmp_path)
    index = json.loads((tmp_path / "index.json").read_text())
    assert index["export_scope"] == "changed_pages"
    assert index["pages_in_export"] == 2
    assert index["counts"]["pages"] == 2  # the tree is always complete


def test_deleted_pages_are_pruned_from_the_json_export(tmp_path, result):
    JsonExporter().export(result, tmp_path)
    stale = next((tmp_path / "pages").glob("Vibration-Limits--*.json"))
    assert stale.exists()

    result.pages = [result.pages[0]]
    result.changes = ChangeSet()
    result.changes.add(PageChange(page_id="pg-1a", status=ChangeStatus.DELETED,
                                  title="Vibration Limits"))
    JsonExporter().export(result, tmp_path)
    assert not stale.exists()
    assert next((tmp_path / "pages").glob("Pump-Commissioning--*.json")).exists()


def test_deleted_subpages_are_pruned_from_the_markdown_export(tmp_path, result):
    MarkdownExporter().export(result, tmp_path)
    stale = next((tmp_path / "markdown").rglob("Vibration-Limits--*.md"))
    assert stale.exists()

    result.pages = [result.pages[0]]
    result.changes = ChangeSet()
    result.changes.add(PageChange(page_id="pg-1a", status=ChangeStatus.DELETED))
    MarkdownExporter().export(result, tmp_path)
    assert not stale.exists()
    # the parent page's now-empty subpage directory is cleaned up too
    assert not stale.parent.exists()


def test_pruning_a_page_that_was_never_exported_is_harmless(tmp_path, result):
    result.changes.add(PageChange(page_id="never-here", status=ChangeStatus.DELETED))
    JsonExporter().export(result, tmp_path)
    assert (tmp_path / "index.json").exists()


# -- resources.json reverse index -------------------------------------------- #


def test_resource_index_links_files_back_to_pages(tmp_path, result):
    shared = dict(file_path="resources/images/abc-shared.png", sha256="a" * 64,
                  resource_id="r-1", mime_type="image/png", byte_size=100, downloaded=True)
    result.pages[0].blocks.append(ImageBlock(order=2, alt_text="curve", source_id="img-1", **shared))
    result.pages[1].blocks.append(ImageBlock(order=1, alt_text="curve", source_id="img-2", **shared))

    JsonExporter().export(result, tmp_path)
    index = json.loads((tmp_path / "resources.json").read_text())

    assert index["counts"]["distinct_resources"] == 1
    assert index["counts"]["images"] == 1
    assert index["counts"]["total_references"] == 2   # deduped file, two referrers
    entry = index["resources"][0]
    assert entry["file_path"] == "resources/images/abc-shared.png"
    assert {r["page_id"] for r in entry["references"]} == {"pg-1", "pg-1a"}
    assert {r["source_id"] for r in entry["references"]} == {"img-1", "img-2"}


def test_resource_index_records_attachment_extraction_state(tmp_path, result):
    from onenote_parser.models import FileBlock

    result.pages[0].blocks.append(FileBlock(
        order=2, file_name="spec.pdf", file_path="resources/attachments/x-spec.pdf",
        sha256="b" * 64, mime_type="application/pdf", byte_size=2048, downloaded=True,
        extracted_text="hello world", extraction_status="ok", extractor="pdf",
    ))
    JsonExporter().export(result, tmp_path)
    entry = json.loads((tmp_path / "resources.json").read_text())["resources"][0]
    assert entry["type"] == "file"
    assert entry["extraction_status"] == "ok" and entry["extractor"] == "pdf"
    assert entry["extracted_chars"] == 11
    assert "extracted_text" not in entry   # the text lives in the page document


def test_resource_index_keeps_failed_downloads_for_retry(tmp_path, result):
    result.pages[0].blocks.append(ImageBlock(
        order=2, resource_id="r-broken", downloaded=False, error="download_failed: 429"))
    JsonExporter().export(result, tmp_path)
    index = json.loads((tmp_path / "resources.json").read_text())
    entry = next(r for r in index["resources"] if r["resource_id"] == "r-broken")
    assert entry["downloaded"] is False and "429" in entry["error"]
    assert index["counts"]["failed"] == 1


def test_resource_index_finds_resources_nested_in_tables(tmp_path, result):
    from onenote_parser.models import TableBlock, TableCell, TableRow

    nested = ImageBlock(resource_id="r-nested", file_path="resources/images/n.png",
                        sha256="c" * 64, downloaded=True)
    result.pages[0].blocks.append(
        TableBlock(order=2, rows=[TableRow(cells=[TableCell(RichText("c"), blocks=[nested])])]))
    JsonExporter().export(result, tmp_path)
    index = json.loads((tmp_path / "resources.json").read_text())
    assert any(r["resource_id"] == "r-nested" for r in index["resources"])


def test_resource_index_is_empty_but_present_with_no_resources(tmp_path, result):
    JsonExporter().export(result, tmp_path)
    index = json.loads((tmp_path / "resources.json").read_text())
    assert index["resources"] == [] and index["counts"]["distinct_resources"] == 0
