"""Recursive discovery of the notebook hierarchy.

Walks ``notebooks -> sectionGroups* -> sections -> pages``.  Section groups nest
arbitrarily deep, so the walk is genuinely recursive rather than one ``$expand``
level.  Nodes the signed-in identity cannot read are recorded with an ``error``
and the crawl continues -- an inaccessible section must never abort a run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import unquote, urlparse

from .config import FilterConfig, ParserConfig
from .errors import AccessDeniedError, GraphHTTPError, NotFoundError
from .graph.client import GraphClient
from .logging_utils import get_logger
from .models import (
    NotebookNode,
    NotebookTree,
    PageRef,
    SectionGroupNode,
    SectionNode,
    parse_graph_datetime,
    utcnow,
)

logger = get_logger("discovery")

__all__ = ["NotebookCrawler", "NameMatcher", "list_recent_notebooks",
           "site_address_from_web_url", "resolve_site_target", "RecentNotebook"]

NOTEBOOK_SELECT = (
    "id,displayName,createdDateTime,lastModifiedDateTime,isDefault,isShared,"
    "links,sectionsUrl,sectionGroupsUrl,userRole,createdBy,lastModifiedBy"
)
SECTION_SELECT = (
    "id,displayName,createdDateTime,lastModifiedDateTime,isDefault,links,pagesUrl,"
    "createdBy,lastModifiedBy"
)
GROUP_SELECT = "id,displayName,createdDateTime,lastModifiedDateTime,sectionsUrl,sectionGroupsUrl"
PAGE_SELECT = (
    "id,title,createdDateTime,lastModifiedDateTime,level,order,contentUrl,links,"
    "createdByAppId"
)

#: Depth guard against pathological or cyclic section-group structures.
MAX_GROUP_DEPTH = 32


@dataclass(slots=True)
class RecentNotebook:
    """A notebook the signed-in user has opened, from any site or group.

    ``/me/onenote/notebooks`` only returns notebooks in the user's own drive.
    ``getRecentNotebooks`` returns everything their OneNote app lists, including
    team and SharePoint notebooks -- which is how you find out *where* a
    notebook lives before pointing ``graph.target`` at it.
    """

    display_name: str = ""
    last_accessed: Optional[datetime] = None
    source_service: Optional[str] = None
    web_url: Optional[str] = None
    client_url: Optional[str] = None

    #: Filled in by :func:`resolve_site_target`; a ready-to-use ``graph.target``.
    resolved_target: Optional[str] = None

    @property
    def site_address(self) -> Optional[str]:
        """The address used to *look up* this notebook's site, if site-hosted."""
        return site_address_from_web_url(self.web_url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "display_name": self.display_name,
            "last_accessed": self.last_accessed.isoformat() if self.last_accessed else None,
            "source_service": self.source_service,
            "web_url": self.web_url,
            "client_url": self.client_url,
            "site_address": self.site_address,
            "target": self.resolved_target,
        }


#: Site path prefixes that delimit a SharePoint site collection.
_SITE_ROOTS = ("/sites/", "/teams/", "/personal/")


def site_address_from_web_url(web_url: Optional[str]) -> Optional[str]:
    """Derive the SharePoint *site lookup* address from a notebook's web URL.

    A notebook URL such as::

        https://contoso.sharepoint.com/sites/Streetlight/SiteAssets/Field%20Notes

    lives in the site ``/sites/Streetlight``; the trailing segments are its
    location inside that site.  The returned value addresses the site for
    ``GET /sites/{address}``.

    This is **not** a ``graph.target``: the OneNote endpoints reject a hostname
    or path address and require the site's composite id.  Pass the result to
    :func:`resolve_site_target` to get a usable target.

    Returns ``None`` for URLs that are not SharePoint-hosted (consumer OneDrive
    or anything unrecognised), where ``me`` is the right target.
    """
    if not web_url:
        return None
    parsed = urlparse(web_url)
    host, path = parsed.hostname, unquote(parsed.path or "")
    if not host or not host.endswith("sharepoint.com"):
        return None

    lowered = path.lower()
    for root in _SITE_ROOTS:
        index = lowered.find(root)
        if index == -1:
            continue
        site_name = path[index + len(root) :].split("/", 1)[0]
        if site_name:
            return f"{host}:{root}{site_name}"
    # No /sites/, /teams/ or /personal/ segment: SharePoint site collections
    # live only at those prefixes or at the root, so this notebook sits in a
    # document library on the root site (e.g. /Tools and Forms/.../Notebook).
    return host


def resolve_site_target(client: GraphClient, web_url: Optional[str]) -> Optional[str]:
    """Turn a notebook web URL into a working ``graph.target``.

    Costs one Graph request.  The OneNote API rejects ``sites/{hostname}`` and
    the ``sites/{hostname}:/path:`` colon syntax with
    ``400 20143 ("The URI segment 'notebooks' is invalid after the segment
    'notes'")`` -- it accepts only the composite
    ``{hostname},{siteCollectionId},{siteId}`` form, so the site must be
    resolved first.

    Returns ``None`` when the URL is not site-hosted or the site is unreadable.
    """
    address = site_address_from_web_url(web_url)
    if not address:
        return None
    try:
        site = client.get_json(f"/sites/{address}", allow_404=True)
    except GraphHTTPError as exc:
        logger.debug("Could not resolve site %s: %s", address, exc)
        return None
    site_id = (site or {}).get("id")
    return f"sites/{site_id}" if site_id else None


def list_recent_notebooks(
    client: GraphClient, *, include_personal: bool = True, resolve_targets: bool = False
) -> list[RecentNotebook]:
    """Notebooks the signed-in user recently opened, across every site.

    One request, plus one per distinct site when ``resolve_targets`` is set -
    site lookups are cached, so several notebooks on the same site cost one
    call between them.
    """
    flag = "true" if include_personal else "false"
    path = f"/onenote/notebooks/getRecentNotebooks(includePersonalNotebooks={flag})"
    payload = client.get_json(path) or {}
    notebooks = []
    for raw in payload.get("value") or []:
        links = raw.get("links") or {}
        notebooks.append(
            RecentNotebook(
                display_name=raw.get("displayName") or "",
                last_accessed=parse_graph_datetime(raw.get("lastAccessedTime")),
                source_service=raw.get("sourceService"),
                web_url=_link(links, "oneNoteWebUrl"),
                client_url=_link(links, "oneNoteClientUrl"),
            )
        )
    notebooks.sort(key=lambda n: n.last_accessed or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)

    if resolve_targets:
        cache: dict[Optional[str], Optional[str]] = {}
        for notebook in notebooks:
            address = notebook.site_address
            if address not in cache:
                cache[address] = resolve_site_target(client, notebook.web_url)
            notebook.resolved_target = cache[address]
    return notebooks


class NameMatcher:
    """Include/exclude matching for notebook and section display names."""

    def __init__(self, include: Sequence[str], exclude: Sequence[str], *, use_regex: bool) -> None:
        self.use_regex = use_regex
        if use_regex:
            self._include = [re.compile(p, re.IGNORECASE) for p in include]
            self._exclude = [re.compile(p, re.IGNORECASE) for p in exclude]
        else:
            self._include = [p.strip().lower() for p in include]  # type: ignore[assignment]
            self._exclude = [p.strip().lower() for p in exclude]  # type: ignore[assignment]

    def matches(self, name: str) -> bool:
        value = (name or "").strip()
        if self.use_regex:
            if any(p.search(value) for p in self._exclude):  # type: ignore[union-attr]
                return False
            if not self._include:
                return True
            return any(p.search(value) for p in self._include)  # type: ignore[union-attr]
        lowered = value.lower()
        if lowered in self._exclude:
            return False
        return not self._include or lowered in self._include


class NotebookCrawler:
    """Builds a :class:`NotebookTree` from Microsoft Graph."""

    def __init__(
        self,
        client: GraphClient,
        config: ParserConfig | None = None,
        *,
        progress: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
    ) -> None:
        self.client = client
        self.config = config or ParserConfig()
        self.filters: FilterConfig = self.config.filters
        self.progress = progress
        self._notebook_matcher = NameMatcher(
            self.filters.notebook_names,
            self.filters.exclude_notebook_names,
            use_regex=self.filters.use_regex,
        )
        self._section_matcher = NameMatcher(
            self.filters.section_names,
            self.filters.exclude_section_names,
            use_regex=self.filters.use_regex,
        )
        self.errors: list[dict[str, Any]] = []

    # -- public API ------------------------------------------------------- #

    def crawl(self) -> NotebookTree:
        """Discover every notebook permitted by the configured filters."""
        tree = NotebookTree(discovered_at=utcnow())
        for raw in self._list_notebooks():
            notebook = self._build_notebook(raw)
            if notebook is not None:
                tree.notebooks.append(notebook)
        counts = tree.counts()
        logger.info(
            "Discovered %d notebook(s), %d section group(s), %d section(s), %d page(s)",
            counts["notebooks"], counts["section_groups"], counts["sections"], counts["pages"],
        )
        return tree

    def crawl_notebook(self, notebook_id: str) -> Optional[NotebookNode]:
        """Discover a single notebook by id."""
        raw = self.client.get_json(
            f"/onenote/notebooks/{self.client.escape(notebook_id)}",
            params={"$select": NOTEBOOK_SELECT},
            allow_404=True,
        )
        if raw is None:
            self._record_error("notebook", notebook_id, "not_found", "Notebook not found")
            return None
        return self._build_notebook(raw, apply_filters=False)

    # -- notebooks -------------------------------------------------------- #

    def _list_notebooks(self) -> Iterable[dict[str, Any]]:
        if self.filters.notebook_ids:
            for notebook_id in self.filters.notebook_ids:
                raw = self._safe_json(
                    f"/onenote/notebooks/{self.client.escape(notebook_id)}",
                    params={"$select": NOTEBOOK_SELECT},
                    kind="notebook",
                    identifier=notebook_id,
                )
                if raw:
                    yield raw
            return
        try:
            yield from self.client.iter_paged(
                "/onenote/notebooks",
                params={"$select": NOTEBOOK_SELECT, "$orderby": "displayName"},
            )
        except GraphHTTPError as exc:
            self._record_error("notebook", "*", self._error_kind(exc), str(exc))
            logger.error("Unable to list notebooks: %s", exc)

    def _build_notebook(
        self, raw: Mapping[str, Any], *, apply_filters: bool = True
    ) -> Optional[NotebookNode]:
        name = raw.get("displayName") or ""
        if apply_filters and not self._notebook_matcher.matches(name):
            logger.debug("Skipping notebook %r (filtered out)", name)
            return None

        links = raw.get("links") or {}
        notebook = NotebookNode(
            id=raw["id"],
            display_name=name,
            created_at=parse_graph_datetime(raw.get("createdDateTime")),
            modified_at=parse_graph_datetime(raw.get("lastModifiedDateTime")),
            web_url=_link(links, "oneNoteWebUrl"),
            client_url=_link(links, "oneNoteClientUrl"),
            is_default=bool(raw.get("isDefault", False)),
            is_shared=bool(raw.get("isShared", False)),
            owner=_identity_name(raw.get("createdBy")),
            last_modified_by=_identity_name(raw.get("lastModifiedBy")),
        )
        self._emit("notebook", {"id": notebook.id, "name": notebook.display_name})
        path = [notebook.display_name]
        path_ids = [notebook.id]

        notebook.sections = self._list_sections(
            f"/onenote/notebooks/{self.client.escape(notebook.id)}/sections",
            notebook=notebook,
            path=path,
            path_ids=path_ids,
            parent_group_id=None,
        )
        notebook.section_groups = self._list_section_groups(
            f"/onenote/notebooks/{self.client.escape(notebook.id)}/sectionGroups",
            notebook=notebook,
            path=path,
            path_ids=path_ids,
            parent_group_id=None,
            depth=0,
        )
        return notebook

    # -- section groups --------------------------------------------------- #

    def _list_section_groups(
        self,
        path_url: str,
        *,
        notebook: NotebookNode,
        path: list[str],
        path_ids: list[str],
        parent_group_id: Optional[str],
        depth: int,
    ) -> list[SectionGroupNode]:
        if depth >= MAX_GROUP_DEPTH:
            logger.warning("Section-group nesting exceeded %d levels at %s; stopping recursion",
                           MAX_GROUP_DEPTH, " > ".join(path))
            self._record_error("section_group", parent_group_id or notebook.id, "max_depth",
                               f"Nesting deeper than {MAX_GROUP_DEPTH} levels")
            return []

        groups: list[SectionGroupNode] = []
        try:
            raw_groups = list(self.client.iter_paged(path_url, params={"$select": GROUP_SELECT}))
        except GraphHTTPError as exc:
            self._record_error("section_group", parent_group_id or notebook.id,
                               self._error_kind(exc), str(exc))
            logger.warning("Cannot list section groups under %s: %s", " > ".join(path), exc)
            return []

        for raw in raw_groups:
            name = raw.get("displayName") or ""
            group = SectionGroupNode(
                id=raw["id"],
                display_name=name,
                created_at=parse_graph_datetime(raw.get("createdDateTime")),
                modified_at=parse_graph_datetime(raw.get("lastModifiedDateTime")),
                web_url=_link(raw.get("links") or {}, "oneNoteWebUrl"),
                notebook_id=notebook.id,
                parent_group_id=parent_group_id,
                path=[*path, name],
                path_ids=[*path_ids, raw["id"]],
            )
            self._emit("section_group", {"id": group.id, "name": name, "path": group.path})
            group.sections = self._list_sections(
                f"/onenote/sectionGroups/{self.client.escape(group.id)}/sections",
                notebook=notebook,
                path=group.path,
                path_ids=group.path_ids,
                parent_group_id=group.id,
            )
            group.section_groups = self._list_section_groups(
                f"/onenote/sectionGroups/{self.client.escape(group.id)}/sectionGroups",
                notebook=notebook,
                path=group.path,
                path_ids=group.path_ids,
                parent_group_id=group.id,
                depth=depth + 1,
            )
            groups.append(group)
        return groups

    # -- sections --------------------------------------------------------- #

    def _list_sections(
        self,
        path_url: str,
        *,
        notebook: NotebookNode,
        path: list[str],
        path_ids: list[str],
        parent_group_id: Optional[str],
    ) -> list[SectionNode]:
        sections: list[SectionNode] = []
        try:
            raw_sections = list(self.client.iter_paged(path_url, params={"$select": SECTION_SELECT}))
        except GraphHTTPError as exc:
            self._record_error("section", parent_group_id or notebook.id,
                               self._error_kind(exc), str(exc))
            logger.warning("Cannot list sections under %s: %s", " > ".join(path), exc)
            return []

        for raw in raw_sections:
            name = raw.get("displayName") or ""
            section_id = raw["id"]
            if self.filters.section_ids and section_id not in self.filters.section_ids:
                continue
            if not self._section_matcher.matches(name):
                logger.debug("Skipping section %r (filtered out)", name)
                continue

            section = SectionNode(
                id=section_id,
                display_name=name,
                created_at=parse_graph_datetime(raw.get("createdDateTime")),
                modified_at=parse_graph_datetime(raw.get("lastModifiedDateTime")),
                web_url=_link(raw.get("links") or {}, "oneNoteWebUrl"),
                client_url=_link(raw.get("links") or {}, "oneNoteClientUrl"),
                is_default=bool(raw.get("isDefault", False)),
                created_by=_identity_name(raw.get("createdBy")),
                last_modified_by=_identity_name(raw.get("lastModifiedBy")),
                notebook_id=notebook.id,
                parent_group_id=parent_group_id,
                path=[*path, name],
                path_ids=[*path_ids, section_id],
            )
            self._emit("section", {"id": section.id, "name": name, "path": section.path})
            section.pages = self._list_pages(section, notebook)
            sections.append(section)
        return sections

    # -- pages ------------------------------------------------------------ #

    def _list_pages(self, section: SectionNode, notebook: NotebookNode) -> list[PageRef]:
        url = f"/onenote/sections/{self.client.escape(section.id)}/pages"
        try:
            raw_pages = list(self.client.iter_paged(url, params={"$select": PAGE_SELECT}))
        except AccessDeniedError as exc:
            section.error = f"inaccessible: {exc.message}"
            self._record_error("section", section.id, "inaccessible", str(exc),
                               path=section.path)
            logger.warning("Section %r is inaccessible: %s", " > ".join(section.path), exc)
            return []
        except NotFoundError as exc:
            section.error = "not_found"
            self._record_error("section", section.id, "not_found", str(exc), path=section.path)
            return []
        except GraphHTTPError as exc:
            section.error = f"error: {exc.message}"
            self._record_error("section", section.id, self._error_kind(exc), str(exc),
                               path=section.path)
            return []

        pages = [
            PageRef(
                id=raw["id"],
                title=raw.get("title") or "",
                level=int(raw.get("level") or 0),
                order=int(raw.get("order") or 0),
                created_at=parse_graph_datetime(raw.get("createdDateTime")),
                modified_at=parse_graph_datetime(raw.get("lastModifiedDateTime")),
                content_url=raw.get("contentUrl"),
                web_url=_link(raw.get("links") or {}, "oneNoteWebUrl"),
                client_url=_link(raw.get("links") or {}, "oneNoteClientUrl"),
                created_by_app_id=raw.get("createdByAppId"),
                section_id=section.id,
                notebook_id=notebook.id,
                path=list(section.path),
                path_ids=list(section.path_ids),
            )
            for raw in raw_pages
        ]
        # Graph does not guarantee ordering across pages of results; sort by the
        # section-relative order so subpage parenting is computed correctly.
        pages.sort(key=lambda p: (p.order, p.created_at.timestamp() if p.created_at else 0.0))
        assign_parent_pages(pages)

        if not self.filters.include_subpages:
            pages = [p for p in pages if p.level == 0]
        if self.filters.page_ids:
            wanted = set(self.filters.page_ids)
            pages = [p for p in pages if p.id in wanted]
        if self.filters.modified_since:
            cutoff = self.filters.modified_since
            pages = [p for p in pages if p.modified_at is None or p.modified_at >= cutoff]
        return pages

    # -- helpers ---------------------------------------------------------- #

    def _safe_json(
        self, path: str, *, params: Mapping[str, Any], kind: str, identifier: str
    ) -> Optional[dict[str, Any]]:
        try:
            return self.client.get_json(path, params=params, allow_404=True)
        except GraphHTTPError as exc:
            self._record_error(kind, identifier, self._error_kind(exc), str(exc))
            logger.warning("Cannot read %s %s: %s", kind, identifier, exc)
            return None

    @staticmethod
    def _error_kind(exc: GraphHTTPError) -> str:
        if isinstance(exc, AccessDeniedError):
            return "inaccessible"
        if isinstance(exc, NotFoundError):
            return "not_found"
        return "graph_error"

    def _record_error(
        self, kind: str, identifier: str, error: str, message: str, *, path: Sequence[str] = ()
    ) -> None:
        self.errors.append(
            {
                "stage": "discovery",
                "kind": kind,
                "id": identifier,
                "error": error,
                "message": message,
                "path": list(path),
            }
        )

    def _emit(self, kind: str, payload: Mapping[str, Any]) -> None:
        if self.progress:
            self.progress(kind, payload)


def assign_parent_pages(pages: Sequence[PageRef]) -> None:
    """Derive ``parent_page_id`` from OneNote's ``level``/``order`` pairs.

    OneNote models subpages as a flat, ordered list where ``level`` is the
    indent depth (0 = top-level page, 1 = subpage, 2 = sub-subpage).  A page's
    parent is therefore the closest preceding page one level up.
    """
    stack: dict[int, str] = {}
    for page in pages:
        level = max(0, page.level)
        page.parent_page_id = stack.get(level - 1) if level > 0 else None
        stack[level] = page.id
        for deeper in [key for key in stack if key > level]:
            del stack[deeper]


def _link(links: Mapping[str, Any], name: str) -> Optional[str]:
    """Extract ``links.<name>.href`` from a Graph payload."""
    value = links.get(name)
    if isinstance(value, Mapping):
        return value.get("href")
    return value if isinstance(value, str) else None


def _identity_name(identity_set: Any) -> Optional[str]:
    if not isinstance(identity_set, Mapping):
        return None
    user = identity_set.get("user") or {}
    if isinstance(user, Mapping):
        return user.get("displayName") or user.get("id")
    return None
