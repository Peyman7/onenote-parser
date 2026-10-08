"""JSON export: an index, a run manifest and one file per page."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import ExportConfig
from ..logging_utils import get_logger
from ..models import ChangeStatus, FileBlock, ImageBlock, ParseResult
from .base import (
    ExportManifest,
    export_scope,
    page_filename,
    remove_page_artifacts,
    write_json,
)

logger = get_logger("export.json")

__all__ = ["JsonExporter"]


class JsonExporter:
    """Writes the canonical, loss-free JSON representation.

    Layout::

        output/
          index.json            hierarchy + counts + run metadata
          manifest.json         stats, change set, errors, file listing
          pages/<slug>.json     one normalized page each (``per_page_files``)
          pages.json            every page in one document (when per-page is off)
    """

    name = "json"

    def __init__(self, config: ExportConfig | None = None) -> None:
        self.config = config or ExportConfig()

    def export(self, result: ParseResult, output_dir: Path) -> ExportManifest:
        output_dir = Path(output_dir)
        manifest = ExportManifest(format=self.name, output_dir=str(output_dir))

        index_path = write_json(
            output_dir / "index.json",
            {
                "schema_version": result.schema_version,
                "run_id": result.run_id,
                "generated_at": result.stats.to_dict()["finished_at"],
                # "changed_pages" means `pages` below is a delta: the hierarchy
                # in `tree` is always complete, but unchanged pages were not
                # re-fetched and so are not re-exported.
                "export_scope": export_scope(result),
                "counts": result.tree.counts(),
                "pages_in_export": len(result.pages),
                "tree": result.tree.to_dict(),
                "pages": [
                    {
                        "id": page.id,
                        "title": page.title,
                        "breadcrumb": page.breadcrumb,
                        "path": page.path,
                        "notebook_id": page.notebook_id,
                        "section_id": page.section_id,
                        "content_hash": page.content_hash,
                        "web_url": page.metadata.web_url,
                        "file": f"pages/{self._page_filename(page.id, page.title)}",
                    }
                    for page in result.pages
                ],
            },
            pretty=self.config.pretty,
        )
        manifest.add(index_path, output_dir)

        if self.config.per_page_files:
            pages_dir = output_dir / "pages"
            for change in result.changes.by_status(ChangeStatus.DELETED):
                for removed in remove_page_artifacts(pages_dir, change.page_id, suffix=".json"):
                    logger.info("Removed export for deleted page %s (%s)",
                                change.page_id, removed.name)
            for page in result.pages:
                path = write_json(
                    pages_dir / self._page_filename(page.id, page.title),
                    page.to_dict(),
                    pretty=self.config.pretty,
                )
                manifest.add(path, output_dir)
        else:
            path = write_json(
                output_dir / "pages.json",
                {
                    "schema_version": result.schema_version,
                    "pages": [p.to_dict() for p in result.pages],
                },
                pretty=self.config.pretty,
            )
            manifest.add(path, output_dir)

        resources_path = write_json(
            output_dir / "resources.json",
            self._resource_index(result),
            pretty=self.config.pretty,
        )
        manifest.add(resources_path, output_dir)

        manifest_path = write_json(
            output_dir / "manifest.json",
            {
                "schema_version": result.schema_version,
                "run_id": result.run_id,
                "stats": result.stats.to_dict(),
                "changes": result.changes.to_dict(),
                "errors": result.errors,
            },
            pretty=self.config.pretty,
        )
        manifest.add(manifest_path, output_dir)
        manifest.page_count = len(result.pages)
        logger.info("JSON export wrote %d file(s) to %s", len(manifest.files), output_dir)
        return manifest

    @staticmethod
    def _resource_index(result: ParseResult) -> dict[str, Any]:
        """Reverse index: every resource, and which pages reference it.

        Page documents point *at* resources via ``file_path``; going the other
        way -- "which pages use this image?" -- needs this index, because
        deduplication makes the relationship one-to-many: one stored file can
        be referenced from any number of pages.

        It is also the work list for a later enrichment pass (OCR, captioning,
        deeper attachment parsing): iterate ``resources``, process each file
        once, then write results back to the page documents using
        ``page_id`` + ``source_id``, which are stable across runs.
        """
        index: dict[str, dict[str, Any]] = {}
        images = attachments = failed = 0

        for page in result.pages:
            for block in page.iter_blocks():
                if not isinstance(block, (ImageBlock, FileBlock)):
                    continue
                # Group by content hash where available; fall back to the Graph
                # resource id so failed downloads still appear and can be retried.
                key = block.sha256 or f"resource:{block.resource_id}" or f"block:{id(block)}"
                entry = index.get(key)
                if entry is None:
                    is_image = isinstance(block, ImageBlock)
                    images += is_image
                    attachments += not is_image
                    failed += not block.downloaded
                    entry = {
                        "key": key,
                        "type": block.type.value,
                        "file_path": block.file_path,
                        "sha256": block.sha256,
                        "resource_id": block.resource_id,
                        "mime_type": block.mime_type,
                        "byte_size": block.byte_size,
                        "downloaded": block.downloaded,
                        "error": block.error,
                        "references": [],
                    }
                    if is_image:
                        entry["is_ink"] = block.is_ink
                        entry["alt_text"] = block.alt_text
                        entry["ocr"] = block.ocr.to_dict() if block.ocr else None
                    else:
                        entry["file_name"] = block.file_name
                        entry["extraction_status"] = block.extraction_status
                        entry["extractor"] = block.extractor
                        entry["extracted_chars"] = len(block.extracted_text or "")
                    index[key] = entry

                entry["references"].append(
                    {
                        "page_id": page.id,
                        "page_title": page.title,
                        "path": list(page.path),
                        "block_order": block.order,
                        "source_id": block.source_id,
                    }
                )

        resources = sorted(index.values(), key=lambda r: (r["type"], r["file_path"] or ""))
        return {
            "schema_version": result.schema_version,
            "run_id": result.run_id,
            "counts": {
                "distinct_resources": len(resources),
                "images": images,
                "attachments": attachments,
                "failed": failed,
                "total_references": sum(len(r["references"]) for r in resources),
                "bytes": sum(r["byte_size"] or 0 for r in resources),
            },
            "resources": resources,
        }

    @staticmethod
    def _page_filename(page_id: str, title: str) -> str:
        """Stable, collision-free filename: ``<title-slug>--<id-hash>.json``."""
        return page_filename(page_id, title, suffix=".json")
