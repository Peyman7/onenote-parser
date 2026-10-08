"""Exporter protocol and shared helpers."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..config import ExportConfig
from ..errors import ExportError
from ..logging_utils import get_logger
from ..models import ParseResult

logger = get_logger("export")

__all__ = ["Exporter", "ExportManifest", "create_exporters", "slugify", "write_json",
           "page_key", "page_filename", "remove_page_artifacts", "export_scope"]

_SLUG_STRIP = re.compile(r"[^\w\s.-]", re.UNICODE)
_SLUG_SPACE = re.compile(r"[\s_]+")


def slugify(value: str, *, fallback: str = "untitled", max_length: int = 80) -> str:
    """Filesystem-safe, stable slug that keeps the text readable.

    Unicode is normalized rather than stripped, so ``Wärmetauscher`` stays
    recognisable instead of collapsing to ``wrmetauscher``.
    """
    text = unicodedata.normalize("NFKC", (value or "").strip())
    text = _SLUG_STRIP.sub("", text)
    text = _SLUG_SPACE.sub("-", text).strip("-.")
    text = re.sub(r"-{2,}", "-", text)
    if not text:
        return fallback
    return text[:max_length].rstrip("-.") or fallback


def page_key(page_id: str, *, length: int = 12) -> str:
    """Short, stable, collision-safe key for a OneNote page id.

    Page ids contain ``!`` and other characters that slugification would strip,
    so two distinct ids could otherwise map to the same filename.  Hashing the
    full id keeps names unique and stable across runs.
    """
    import hashlib

    return hashlib.sha256((page_id or "").encode("utf-8")).hexdigest()[:length]


def page_filename(page_id: str, title: str, *, suffix: str) -> str:
    """``<readable-title>--<hash>.<ext>`` - readable and guaranteed unique."""
    return f"{slugify(title, fallback='page', max_length=60)}--{page_key(page_id)}{suffix}"


def remove_page_artifacts(root: Path, page_id: str, *, suffix: str) -> list[Path]:
    """Delete a deleted page's exported files.

    Files are matched on the page-id hash embedded in every exported filename,
    so a page is found wherever it sits in the mirrored hierarchy - including
    when it was a subpage nested under its parent's directory.  Without this, a
    page removed in OneNote would linger in the output forever and downstream
    consumers would keep serving content that no longer exists.
    """
    if not root.exists():
        return []
    removed: list[Path] = []
    for path in list(root.rglob(f"*--{page_key(page_id)}{suffix}")):
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:  # pragma: no cover - permissions/races
            logger.warning("Could not remove stale export %s: %s", path, exc)
    # The parent page's subpage directory may now be empty.  Only ever prune a
    # page-derived directory (its name carries the "--<hash>" suffix), never a
    # notebook or section directory that merely happens to be empty.
    for path in removed:
        directory = path.parent
        if "--" not in directory.name or not directory.is_dir():
            continue
        if not any(directory.iterdir()):
            try:
                directory.rmdir()
            except OSError:  # pragma: no cover - races
                pass
    return removed


def export_scope(result: "ParseResult") -> str:
    """Whether this export carries every discovered page or only the changed ones.

    An incremental run deliberately does not re-fetch unchanged pages, so its
    export is a *delta*.  Labelling it prevents a consumer from mistaking a
    partial catalogue for the whole corpus.
    """
    return "all_pages" if len(result.pages) >= result.stats.pages_discovered else "changed_pages"


def write_json(path: Path, payload: Any, *, pretty: bool = True) -> Path:
    """Write JSON to ``path``, creating parents.  Raises :class:`ExportError`."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as stream:
            json.dump(
                payload, stream, ensure_ascii=False,
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":"),
            )
            stream.write("\n")
    except (OSError, TypeError, ValueError) as exc:
        raise ExportError(f"Could not write {path}: {exc}", details={"path": str(path)}) from exc
    return path


@dataclass
class ExportManifest:
    """Index of everything an export produced."""

    format: str
    output_dir: str
    files: list[str] = field(default_factory=list)
    page_count: int = 0
    bytes_written: int = 0

    def add(self, path: Path, root: Path) -> None:
        self.files.append(_relative(path, root))
        try:
            self.bytes_written += path.stat().st_size
        except OSError:  # pragma: no cover - file vanished under us
            pass

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "output_dir": self.output_dir,
            "page_count": self.page_count,
            "file_count": len(self.files),
            "bytes_written": self.bytes_written,
            "files": self.files,
        }


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


@runtime_checkable
class Exporter(Protocol):
    """Writes a :class:`ParseResult` to disk in one format."""

    name: str

    def export(self, result: ParseResult, output_dir: Path) -> ExportManifest:
        ...


def create_exporters(config: ExportConfig) -> list[Exporter]:
    """Build the exporters named in ``export.formats``."""
    from .json_export import JsonExporter
    from .jsonl_export import JsonlExporter
    from .markdown_export import MarkdownExporter

    registry: dict[str, Any] = {
        "json": JsonExporter,
        "jsonl": JsonlExporter,
        "markdown": MarkdownExporter,
    }
    exporters: list[Exporter] = []
    for name in config.formats:
        factory = registry.get(name)
        if factory is None:
            raise ExportError(f"Unknown export format {name!r}; valid: {sorted(registry)}")
        exporters.append(factory(config))
    return exporters
