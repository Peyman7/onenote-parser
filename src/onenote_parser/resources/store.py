"""Content-addressed file store shared by the image and attachment processors."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

from ..errors import ResourceError
from ..logging_utils import get_logger

logger = get_logger("resources.store")

__all__ = ["ResourceStore", "sha256_hex", "safe_filename", "extension_for_mime"]

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

#: Minimal, explicit MIME -> extension map.  ``mimetypes`` is consulted as a
#: fallback but its answers vary by platform, which would make file names
#: unstable across machines.
MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/x-emf": ".emf",
    "image/x-wmf": ".wmf",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/zip": ".zip",
    "application/json": ".json",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/html": ".html",
    "text/markdown": ".md",
}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_filename(name: str, *, fallback: str = "resource", max_length: int = 96) -> str:
    """Sanitize a filename for cross-platform, path-traversal-safe storage."""
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    cleaned = _UNSAFE.sub("_", base).strip("._")
    if not cleaned:
        cleaned = fallback
    stem, dot, suffix = cleaned.rpartition(".")
    if dot and len(suffix) <= 8:
        stem = stem[: max_length - len(suffix) - 1] or fallback
        return f"{stem}.{suffix}"
    return cleaned[:max_length]


def extension_for_mime(mime_type: Optional[str], *, default: str = ".bin") -> str:
    if not mime_type:
        return default
    key = mime_type.split(";")[0].strip().lower()
    if key in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[key]
    import mimetypes

    return mimetypes.guess_extension(key) or default


class ResourceStore:
    """Writes resource bytes under ``root``, de-duplicating by content hash.

    Files are named ``<sha256[:16]>-<original-name>`` so that the same image
    referenced from twenty pages is stored once, while the name still hints at
    what it is.  :meth:`store` is thread-safe and idempotent.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        deduplicate: bool = True,
        index_path: str | Path | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self.deduplicate = deduplicate
        self._lock = threading.Lock()
        self._by_hash: dict[str, Path] = {}
        # Resource-id index: lets a re-run skip downloading bytes it already
        # holds.  Content-hash dedup cannot do that on its own, because the
        # hash is only knowable *after* the download.
        self.index_path = Path(index_path).expanduser() if index_path else None
        self._index: dict[str, dict[str, Any]] = {}
        self._pending = 0
        self._load_index()

    # -- resource-id index ------------------------------------------------ #

    def _load_index(self) -> None:
        if not self.index_path or not self.index_path.exists():
            return
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self._index = payload.get("resources") or {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable resource index %s: %s", self.index_path, exc)

    def lookup(self, resource_id: Optional[str]) -> Optional[dict[str, Any]]:
        """Return a previously stored resource, if its file is still present."""
        if not resource_id or not self.index_path:
            return None
        with self._lock:
            entry = self._index.get(resource_id)
        if not entry:
            return None
        path = self.root.parent / entry["file_path"] if entry.get("file_path") else None
        if path is None or not path.exists():
            return None
        return entry

    def remember(self, resource_id: Optional[str], entry: dict[str, Any]) -> None:
        if not resource_id or not self.index_path:
            return
        with self._lock:
            self._index[resource_id] = entry
            self._pending += 1
            flush = self._pending >= 25
        if flush:
            self.save_index()

    def save_index(self) -> None:
        """Persist the index atomically so an interrupted run keeps its progress."""
        if not self.index_path:
            return
        with self._lock:
            payload = {"version": 1, "resources": dict(self._index)}
            self._pending = 0
        try:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.index_path.with_suffix(self.index_path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.index_path)
        except OSError as exc:  # pragma: no cover - disk full / permissions
            logger.warning("Could not write resource index %s: %s", self.index_path, exc)

    def store(
        self,
        data: bytes,
        *,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        digest: Optional[str] = None,
    ) -> tuple[Path, str, bool]:
        """Persist ``data``.  Returns ``(path, sha256, reused)``."""
        checksum = digest or sha256_hex(data)
        with self._lock:
            existing = self._by_hash.get(checksum)
            if self.deduplicate and existing is not None and existing.exists():
                return existing, checksum, True

            name = safe_filename(filename or "") if filename else ""
            if not name or "." not in name:
                name = (name or "resource") + extension_for_mime(mime_type)
            target = self.root / f"{checksum[:16]}-{name}"
            if self.deduplicate and target.exists() and target.stat().st_size == len(data):
                self._by_hash[checksum] = target
                return target, checksum, True

            try:
                self.root.mkdir(parents=True, exist_ok=True)
                # Write to a temp name then replace, so a crash never leaves a
                # half-written file that a later run would treat as complete.
                temporary = target.with_suffix(target.suffix + ".part")
                temporary.write_bytes(data)
                temporary.replace(target)
            except OSError as exc:
                raise ResourceError(
                    f"Could not write resource to {target}: {exc}",
                    details={"path": str(target)},
                ) from exc

            self._by_hash[checksum] = target
            return target, checksum, False

    def relative(self, path: Path, *, base: Optional[Path] = None) -> str:
        """POSIX-style path relative to ``base`` (default: the store root's parent)."""
        anchor = base or self.root.parent
        try:
            return path.relative_to(anchor).as_posix()
        except ValueError:
            return path.as_posix()
