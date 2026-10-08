"""JSONL export: one page per line, for streaming ingestion."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import ExportConfig
from ..errors import ExportError
from ..logging_utils import get_logger
from ..models import ParseResult

from .base import ExportManifest

logger = get_logger("export.jsonl")

__all__ = ["JsonlExporter"]


class JsonlExporter:
    """Writes ``pages.jsonl`` (and ``changes.jsonl``) for stream consumers.

    Each line is a complete, self-describing page document carrying its schema
    version, breadcrumb and hierarchy ids -- the shape downstream chunkers and
    indexers want to read with a single pass and no random access.
    """

    name = "jsonl"

    def __init__(self, config: ExportConfig | None = None) -> None:
        self.config = config or ExportConfig()

    def export(self, result: ParseResult, output_dir: Path) -> ExportManifest:
        output_dir = Path(output_dir)
        manifest = ExportManifest(format=self.name, output_dir=str(output_dir))
        pages_path = output_dir / "pages.jsonl"
        changes_path = output_dir / "changes.jsonl"

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            with pages_path.open("w", encoding="utf-8") as stream:
                for page in result.pages:
                    stream.write(json.dumps(page.to_dict(), ensure_ascii=False,
                                            separators=(",", ":")))
                    stream.write("\n")
            with changes_path.open("w", encoding="utf-8") as stream:
                for change in result.changes.changes:
                    stream.write(json.dumps(change.to_dict(), ensure_ascii=False,
                                            separators=(",", ":")))
                    stream.write("\n")
        except OSError as exc:
            raise ExportError(f"Could not write JSONL export: {exc}",
                              details={"path": str(pages_path)}) from exc

        manifest.add(pages_path, output_dir)
        manifest.add(changes_path, output_dir)
        manifest.page_count = len(result.pages)
        logger.info("JSONL export wrote %d page(s) to %s", len(result.pages), pages_path)
        return manifest
