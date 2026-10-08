"""Markdown export: a browsable mirror of the notebook hierarchy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import ExportConfig
from ..errors import ExportError
from ..logging_utils import get_logger
from ..models import ParsedPage, ParseResult
from ..parsing.normalize import render_markdown
from ..models import ChangeStatus
from .base import ExportManifest, page_filename, remove_page_artifacts, slugify

logger = get_logger("export.markdown")

__all__ = ["MarkdownExporter"]


class MarkdownExporter:
    """Writes one ``.md`` per page plus a ``README.md`` table of contents.

    With ``markdown_mirror_hierarchy`` the directory layout mirrors
    ``Notebook/Section group/Section/Page.md``, so the export is directly
    browsable, diffable in git, and ready for migration into a wiki.  Subpages
    are nested under their parent's directory.
    """

    name = "markdown"

    def __init__(self, config: ExportConfig | None = None) -> None:
        self.config = config or ExportConfig()

    def export(self, result: ParseResult, output_dir: Path) -> ExportManifest:
        root = Path(output_dir) / "markdown"
        manifest = ExportManifest(format=self.name, output_dir=str(root))
        written: list[tuple[ParsedPage, Path]] = []

        for change in result.changes.by_status(ChangeStatus.DELETED):
            for removed in remove_page_artifacts(root, change.page_id, suffix=".md"):
                logger.info("Removed Markdown for deleted page %s (%s)",
                            change.page_id, removed.name)

        # Pre-compute paths so parents are known before subpages are written.
        page_paths: dict[str, Path] = {}
        for page in result.pages:
            page_paths[page.id] = self._page_path(page, root, page_paths)

        for page in result.pages:
            path = page_paths[page.id]
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self._render(page, root), encoding="utf-8")
            except OSError as exc:
                raise ExportError(f"Could not write {path}: {exc}",
                                  details={"path": str(path)}) from exc
            manifest.add(path, root)
            written.append((page, path))

        toc = root / "README.md"
        try:
            toc.write_text(self._render_toc(result, written, root), encoding="utf-8")
        except OSError as exc:
            raise ExportError(f"Could not write {toc}: {exc}") from exc
        manifest.add(toc, root)
        manifest.page_count = len(result.pages)
        logger.info("Markdown export wrote %d page(s) to %s", len(result.pages), root)
        return manifest

    # -- layout ----------------------------------------------------------- #

    def _page_path(self, page: ParsedPage, root: Path, known: dict[str, Path]) -> Path:
        name = page_filename(page.id, page.title, suffix=".md")
        if not self.config.markdown_mirror_hierarchy:
            return root / name
        parent_id = page.metadata.parent_page_id
        if parent_id and parent_id in known:
            # Subpage: live in a folder named after the parent page.
            parent_path = known[parent_id]
            return parent_path.with_suffix("") / name
        directory = root
        for part in page.path:
            directory = directory / slugify(part, fallback="section")
        return directory / name

    # -- rendering -------------------------------------------------------- #

    def _render(self, page: ParsedPage, root: Path) -> str:
        parts: list[str] = []
        if self.config.markdown_front_matter:
            parts.append(self._front_matter(page))
        parts.append(f"# {page.title or 'Untitled'}\n")
        parts.append(f"> {' › '.join(page.path)}\n" if page.path else "")
        body = render_markdown(page.blocks)
        parts.append(body)
        if page.warnings:
            parts.append("\n---\n")
            parts.append("**Parser warnings:** " + ", ".join(sorted(set(page.warnings))))
        return "\n".join(p for p in parts if p is not None).rstrip() + "\n"

    @staticmethod
    def _front_matter(page: ParsedPage) -> str:
        """YAML front matter written without a YAML dependency."""
        fields: list[tuple[str, Any]] = [
            ("id", page.id),
            ("title", page.title),
            ("notebook", page.notebook_name),
            ("section", page.section_name),
            ("path", page.path),
            ("created_at", page.metadata.to_dict()["created_at"]),
            ("modified_at", page.metadata.to_dict()["modified_at"]),
            ("last_modified_by", page.metadata.last_modified_by),
            ("web_url", page.metadata.web_url),
            ("content_hash", page.content_hash),
            ("schema_version", page.schema_version),
        ]
        lines = ["---"]
        for key, value in fields:
            if value in (None, "", []):
                continue
            if isinstance(value, list):
                lines.append(f"{key}:")
                lines.extend(f"  - {json.dumps(item, ensure_ascii=False)}" for item in value)
            else:
                lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        flags = {k: v for k, v in page.flags.to_dict().items() if v}
        if flags:
            lines.append("flags:")
            lines.extend(f"  - {name}" for name in sorted(flags))
        lines.append("---\n")
        return "\n".join(lines)

    def _render_toc(
        self, result: ParseResult, written: list[tuple[ParsedPage, Path]], root: Path
    ) -> str:
        counts = result.tree.counts()
        lines = [
            "# OneNote export",
            "",
            f"- Run: `{result.run_id}`",
            f"- Notebooks: {counts['notebooks']} · Section groups: {counts['section_groups']} · "
            f"Sections: {counts['sections']} · Pages: {len(result.pages)}",
            f"- Schema version: `{result.schema_version}`",
            "",
            "## Contents",
            "",
        ]
        current_path: list[str] = []
        for page, path in written:
            if page.path != current_path:
                current_path = list(page.path)
                lines.append("")
                lines.append(f"**{' › '.join(current_path)}**")
                lines.append("")
            indent = "  " * max(0, page.metadata.level)
            relative = path.relative_to(root).as_posix()
            lines.append(f"{indent}- [{page.title or 'Untitled'}]({relative})")

        changes = result.changes.counts()
        if any(changes.values()):
            lines.extend(["", "## Changes since last run", ""])
            lines.extend(f"- {name}: {count}" for name, count in changes.items() if count)
        return "\n".join(lines) + "\n"
