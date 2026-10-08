"""Incremental-sync state and change detection.

The store answers two questions:

1. *Before* fetching a page - "has ``lastModifiedDateTime`` moved since the last
   run?"  A cheap timestamp check lets an incremental run skip the expensive
   ``/content`` call entirely, which is what keeps the parser under OneNote's
   throttling limits.
2. *After* parsing - "did the normalized content actually change?"  The content
   hash catches real edits and ignores no-op saves, and a page that moved or was
   renamed is correctly reported as modified.

Pages present in the previous state but absent now are reported as ``deleted``;
pages that raised 401/403 are reported as ``inaccessible`` and (by default) kept
in the state so a permission change does not look like a deletion.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .config import StateConfig
from .errors import StateError
from .logging_utils import get_logger
from .models import (
    ChangeSet,
    ChangeStatus,
    PageChange,
    PageRef,
    ParsedPage,
    parse_graph_datetime,
    utcnow,
)

logger = get_logger("state")

__all__ = ["PageState", "StateStore", "STATE_VERSION"]

STATE_VERSION = 1


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if value else None


@dataclass(slots=True)
class PageState:
    """What the previous run recorded about one page."""

    page_id: str
    content_hash: str = ""
    modified_at: Optional[datetime] = None
    title: str = ""
    path: list[str] = field(default_factory=list)
    section_id: Optional[str] = None
    notebook_id: Optional[str] = None
    status: str = ChangeStatus.NEW.value
    last_seen_run: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    resource_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "content_hash": self.content_hash,
            "modified_at": _iso(self.modified_at),
            "title": self.title,
            "path": list(self.path),
            "section_id": self.section_id,
            "notebook_id": self.notebook_id,
            "status": self.status,
            "last_seen_run": self.last_seen_run,
            "last_seen_at": _iso(self.last_seen_at),
            "resource_count": self.resource_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PageState":
        return cls(
            page_id=data["page_id"],
            content_hash=data.get("content_hash", ""),
            modified_at=parse_graph_datetime(data.get("modified_at")),
            title=data.get("title", ""),
            path=list(data.get("path") or []),
            section_id=data.get("section_id"),
            notebook_id=data.get("notebook_id"),
            status=data.get("status", ChangeStatus.NEW.value),
            last_seen_run=data.get("last_seen_run"),
            last_seen_at=parse_graph_datetime(data.get("last_seen_at")),
            resource_count=int(data.get("resource_count") or 0),
        )


class StateStore:
    """JSON-backed page state.

    The default backend is a single JSON file, chosen so the parser has no
    infrastructure dependency.  Swap in Azure Table Storage, DynamoDB or a
    database by subclassing and overriding :meth:`load` and :meth:`save`.
    """

    def __init__(self, path: str | Path | None, config: Optional[StateConfig] = None) -> None:
        self.config = config or StateConfig()
        self.path = Path(path).expanduser() if path else None
        self._pages: dict[str, PageState] = {}
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self.loaded_at: Optional[datetime] = None
        self.previous_run_id: Optional[str] = None

    # -- persistence ------------------------------------------------------ #

    def load(self) -> "StateStore":
        if not self.path or not self.path.exists():
            logger.debug("No existing state at %s; treating every page as new", self.path)
            return self
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(
                f"State file {self.path} is unreadable: {exc}. Delete it to force a full run.",
                details={"path": str(self.path)},
            ) from exc
        version = int(payload.get("version") or 0)
        if version > STATE_VERSION:
            raise StateError(
                f"State file {self.path} was written by a newer version "
                f"(v{version} > v{STATE_VERSION})",
                details={"path": str(self.path), "version": version},
            )
        self._pages = {
            page_id: PageState.from_dict(entry)
            for page_id, entry in (payload.get("pages") or {}).items()
        }
        self.loaded_at = parse_graph_datetime(payload.get("updated_at"))
        self.previous_run_id = payload.get("run_id")
        logger.info("Loaded state for %d page(s) from %s", len(self._pages), self.path)
        return self

    def save(self, *, run_id: str = "") -> None:
        """Atomically write the state file (temp file + ``os.replace``)."""
        if not self.path:
            return
        payload = {
            "version": STATE_VERSION,
            "run_id": run_id,
            "updated_at": _iso(utcnow()),
            "page_count": len(self._pages),
            "pages": {page_id: state.to_dict() for page_id, state in sorted(self._pages.items())},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent), delete=False, suffix=".tmp"
            )
            with handle as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(handle.name, self.path)
        except OSError as exc:
            raise StateError(f"Could not write state to {self.path}: {exc}") from exc
        logger.info("Wrote state for %d page(s) to %s", len(self._pages), self.path)

    # -- queries ---------------------------------------------------------- #

    def __len__(self) -> int:
        return len(self._pages)

    def get(self, page_id: str) -> Optional[PageState]:
        return self._pages.get(page_id)

    def known_page_ids(self) -> set[str]:
        return set(self._pages)

    def should_fetch(self, page: PageRef) -> tuple[bool, Optional[ChangeStatus]]:
        """Cheap pre-check performed before downloading page content.

        Returns ``(fetch, presumed_status)``.  ``presumed_status`` is only set
        when the fetch can be skipped.
        """
        if not self.config.enabled or self.config.mode != "incremental":
            return True, None
        previous = self._pages.get(page.id)
        if previous is None or not previous.content_hash:
            return True, None
        if previous.modified_at is None or page.modified_at is None:
            return True, None
        if page.modified_at > previous.modified_at:
            return True, None
        # Timestamp unchanged, but a rename or a move still counts as a change
        # and must be re-exported so paths stay correct downstream.
        if previous.title != page.title or previous.path != list(page.path):
            return True, None
        return False, ChangeStatus.UNCHANGED

    # -- recording -------------------------------------------------------- #

    def mark_seen(self, page_id: str) -> None:
        with self._lock:
            self._seen.add(page_id)

    def record_parsed(self, page: ParsedPage, *, run_id: str = "") -> PageChange:
        """Record a freshly parsed page and classify the change."""
        with self._lock:
            previous = self._pages.get(page.id)
            self._seen.add(page.id)

            if previous is None:
                status = ChangeStatus.NEW
                reason = "not_in_previous_state"
            elif previous.content_hash != page.content_hash:
                status = ChangeStatus.MODIFIED
                reason = self._modification_reason(previous, page)
            else:
                status = ChangeStatus.UNCHANGED
                reason = None

            self._pages[page.id] = PageState(
                page_id=page.id,
                content_hash=page.content_hash,
                modified_at=page.metadata.modified_at,
                title=page.title,
                path=list(page.path),
                section_id=page.section_id,
                notebook_id=page.notebook_id,
                status=status.value,
                last_seen_run=run_id,
                last_seen_at=utcnow(),
                resource_count=sum(
                    1 for b in page.iter_blocks() if b.type.value in ("image", "file")
                ),
            )

        return PageChange(
            page_id=page.id,
            status=status,
            title=page.title,
            path=list(page.path),
            previous_hash=previous.content_hash if previous else None,
            current_hash=page.content_hash,
            previous_modified_at=previous.modified_at if previous else None,
            current_modified_at=page.metadata.modified_at,
            reason=reason,
        )

    @staticmethod
    def _modification_reason(previous: PageState, page: ParsedPage) -> str:
        if previous.title != page.title:
            return "title_changed"
        if previous.path != list(page.path):
            return "moved"
        return "content_changed"

    def record_skipped(self, page: PageRef, *, run_id: str = "") -> PageChange:
        """Record a page whose fetch was skipped because it is unchanged."""
        with self._lock:
            self._seen.add(page.id)
            previous = self._pages.get(page.id)
            if previous is not None:
                previous.status = ChangeStatus.UNCHANGED.value
                previous.last_seen_run = run_id
                previous.last_seen_at = utcnow()
        return PageChange(
            page_id=page.id,
            status=ChangeStatus.UNCHANGED,
            title=page.title,
            path=list(page.path),
            previous_hash=previous.content_hash if previous else None,
            current_hash=previous.content_hash if previous else None,
            previous_modified_at=previous.modified_at if previous else None,
            current_modified_at=page.modified_at,
            reason="not_modified_since_last_run",
        )

    def record_inaccessible(self, page: PageRef, reason: str, *, run_id: str = "") -> PageChange:
        """Record a page the identity may no longer read (401/403)."""
        with self._lock:
            self._seen.add(page.id)
            previous = self._pages.get(page.id)
            if previous is not None:
                previous.status = ChangeStatus.INACCESSIBLE.value
                previous.last_seen_run = run_id
                previous.last_seen_at = utcnow()
            elif self.config.retain_inaccessible:
                self._pages[page.id] = PageState(
                    page_id=page.id,
                    title=page.title,
                    path=list(page.path),
                    section_id=page.section_id,
                    notebook_id=page.notebook_id,
                    modified_at=page.modified_at,
                    status=ChangeStatus.INACCESSIBLE.value,
                    last_seen_run=run_id,
                    last_seen_at=utcnow(),
                )
        return PageChange(
            page_id=page.id,
            status=ChangeStatus.INACCESSIBLE,
            title=page.title,
            path=list(page.path),
            previous_hash=previous.content_hash if previous else None,
            current_modified_at=page.modified_at,
            reason=reason,
        )

    def detect_deletions(self, *, prune: bool = True) -> list[PageChange]:
        """Pages known previously but not seen in this run.

        Only meaningful after a full crawl; a filtered run would otherwise
        report every unvisited page as deleted, so the pipeline suppresses this
        when filters are active.
        """
        if not self.config.detect_deletions:
            return []
        changes: list[PageChange] = []
        with self._lock:
            missing = [pid for pid in self._pages if pid not in self._seen]
            for page_id in missing:
                previous = self._pages[page_id]
                if previous.status == ChangeStatus.INACCESSIBLE.value \
                        and self.config.retain_inaccessible:
                    # Never silently downgrade "you lost access" to "it is gone".
                    continue
                changes.append(
                    PageChange(
                        page_id=page_id,
                        status=ChangeStatus.DELETED,
                        title=previous.title,
                        path=list(previous.path),
                        previous_hash=previous.content_hash,
                        previous_modified_at=previous.modified_at,
                        reason="absent_from_current_crawl",
                    )
                )
                if prune:
                    del self._pages[page_id]
        return changes

    def to_change_set(self, changes: Iterable[PageChange]) -> ChangeSet:
        result = ChangeSet()
        for change in changes:
            result.add(change)
        return result
