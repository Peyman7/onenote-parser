"""The :class:`OneNoteParser` facade - the package's primary entry point.

    from onenote_parser import OneNoteParser, ParserConfig

    config = ParserConfig.load("config.yaml")
    with OneNoteParser(config) as parser:
        result = parser.run()
        print(result.changes.counts())

Every stage is also usable on its own -- :meth:`discover`, :meth:`parse_page`,
:meth:`export` -- so a host application can drive the parts it needs (for
example, discovery only, or re-parsing a single page) without the full run.

Concurrency
-----------
Page fetch/parse is spread across ``runtime.concurrency`` threads and the
results are re-ordered afterwards, so the output is deterministic regardless of
worker scheduling.  The Graph client's shared token bucket keeps the aggregate
request rate under the configured ceiling no matter how many workers run.
"""

from __future__ import annotations

import concurrent.futures
import uuid
from email.parser import BytesParser
from email.policy import default as default_email_policy
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .auth import TokenProvider, create_token_provider
from .config import ParserConfig
from .discovery import NotebookCrawler
from .errors import (
    AccessDeniedError,
    GraphHTTPError,
    NotFoundError,
    OneNoteParserError,
    ParseError,
)
from .export import Exporter, ExportManifest, create_exporters
from .graph.client import GraphClient
from .logging_utils import get_logger
from .models import (
    ChangeSet,
    ChangeStatus,
    FileBlock,
    ImageBlock,
    NotebookTree,
    PageMetadata,
    PageRef,
    ParsedPage,
    ParseResult,
    RunStats,
    utcnow,
)
from .parsing import OneNoteHtmlParser
from .parsing.normalize import compute_content_hash, render_text, summarize_flags
from .resources import (
    AttachmentProcessor,
    ImageProcessor,
    OcrCache,
    OcrEngine,
    ResourceStore,
    create_ocr_engine,
)
from .state import StateStore

logger = get_logger("pipeline")

__all__ = ["OneNoteParser"]

ProgressCallback = Callable[[str, Mapping[str, Any]], None]


class OneNoteParser:
    """Discovers, retrieves, parses, enriches and exports OneNote notebooks."""

    def __init__(
        self,
        config: Optional[ParserConfig] = None,
        *,
        token_provider: Optional[TokenProvider] = None,
        client: Optional[GraphClient] = None,
        ocr_engine: Optional[OcrEngine] = None,
        state_store: Optional[StateStore] = None,
        exporters: Optional[Sequence[Exporter]] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> None:
        self.config = config or ParserConfig()
        self.warnings = self.config.validate()
        for warning in self.warnings:
            logger.warning("Configuration: %s", warning)

        self.progress = progress
        self._owns_client = client is None
        if client is not None:
            self.client = client
        else:
            provider = token_provider or create_token_provider(self.config)
            self.client = GraphClient(provider, self.config.graph)

        self.html_parser = OneNoteHtmlParser(self.config.content)
        self.state = state_store or StateStore(
            self.config.state.path if self.config.state.enabled else None,
            self.config.state,
        )
        self.exporters = list(exporters) if exporters is not None else create_exporters(
            self.config.export
        )

        output_root = self.config.output_path
        self.image_store = ResourceStore(
            self.config.resolve_output(self.config.images.directory),
            deduplicate=self.config.images.deduplicate,
            index_path=self.config.resolve_output("resources/.image-index.json"),
        )
        self.attachment_store = ResourceStore(
            self.config.resolve_output(self.config.attachments.directory),
            deduplicate=self.config.attachments.deduplicate,
            index_path=self.config.resolve_output("resources/.attachment-index.json"),
        )
        self.ocr_cache = (
            OcrCache(self.config.resolve_output(self.config.ocr.cache_path))
            if self.config.ocr.enabled and self.config.ocr.cache_path
            else None
        )
        self.ocr_engine = ocr_engine or create_ocr_engine(self.config.ocr)
        self.images = ImageProcessor(
            self.client, self.config.images, self.image_store,
            ocr_engine=self.ocr_engine, ocr_config=self.config.ocr,
            ocr_cache=self.ocr_cache, output_root=output_root,
        )
        self.attachments = AttachmentProcessor(
            self.client, self.config.attachments, self.attachment_store,
            output_root=output_root,
        )
        self.errors: list[dict[str, Any]] = []

    # -- lifecycle -------------------------------------------------------- #

    def close(self) -> None:
        self.image_store.save_index()
        self.attachment_store.save_index()
        if self.ocr_cache:
            self.ocr_cache.save()
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "OneNoteParser":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- stage 1: discovery ----------------------------------------------- #

    def discover(self) -> NotebookTree:
        """Walk the notebook hierarchy without fetching any page content."""
        crawler = NotebookCrawler(self.client, self.config, progress=self.progress)
        tree = crawler.crawl()
        self.errors.extend(crawler.errors)
        return tree

    # -- stage 2: a single page ------------------------------------------- #

    def fetch_page_html(self, page: PageRef | str) -> tuple[str, list[tuple[str, bytes]]]:
        """Download a page's HTML (and InkML parts when configured).

        Returns ``(html, ink_parts)`` where each ink part is
        ``(content_type, bytes)``.
        """
        page_id = page if isinstance(page, str) else page.id
        content_url = None if isinstance(page, str) else page.content_url
        url = content_url or f"/onenote/pages/{self.client.escape(page_id)}/content"

        params: dict[str, Any] = {}
        if self.config.content.include_ids:
            params["includeIDs"] = "true"
        if self.config.content.include_ink_ml:
            params["includeinkml"] = "true"

        response = self.client.request("GET", url, params=params, accept="text/html")
        content_type = response.content_type
        if content_type.startswith("multipart/"):
            return self._split_multipart(response.content, response.headers.get("content-type", ""))
        return response.text(), []

    @staticmethod
    def _split_multipart(body: bytes, content_type: str) -> tuple[str, list[tuple[str, bytes]]]:
        """Split a ``multipart/form-data`` page response into HTML + InkML."""
        message = BytesParser(policy=default_email_policy).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        )
        html = ""
        parts: list[tuple[str, bytes]] = []
        for part in message.walk():
            if part.is_multipart():
                continue
            part_type = (part.get_content_type() or "").lower()
            payload = part.get_payload(decode=True) or b""
            if part_type in ("text/html", "application/xhtml+xml") and not html:
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="replace")
            elif payload:
                parts.append((part_type, payload))
        return html, parts

    def parse_page(self, page: PageRef | str, *, download_resources: bool = True) -> ParsedPage:
        """Fetch, parse and enrich a single page."""
        page_ref = page if isinstance(page, PageRef) else PageRef(id=page)
        html, ink_parts = self.fetch_page_html(page_ref)
        return self.parse_page_html(
            page_ref, html, ink_parts=ink_parts, download_resources=download_resources
        )

    def parse_page_html(
        self,
        page_ref: PageRef,
        html: str,
        *,
        ink_parts: Sequence[tuple[str, bytes]] = (),
        download_resources: bool = True,
    ) -> ParsedPage:
        """Normalize already-downloaded HTML.

        Exposed separately so cached or archived HTML can be re-parsed offline
        without touching Microsoft Graph.
        """
        parsed = self.html_parser.parse(html, page_id=page_ref.id)
        blocks = parsed.blocks

        if download_resources:
            self._process_resources(blocks, page_ref)
        if ink_parts:
            blocks.extend(self._store_ink(ink_parts, page_ref, start_order=len(blocks)))

        for index, block in enumerate(blocks):
            block.order = index

        text, truncated = render_text(blocks, max_chars=self.config.content.max_text_chars)
        title = page_ref.title or parsed.title or ""
        page = ParsedPage(
            id=page_ref.id,
            title=title,
            notebook_id=page_ref.notebook_id,
            notebook_name=page_ref.path[0] if page_ref.path else None,
            section_id=page_ref.section_id,
            section_name=page_ref.path[-1] if page_ref.path else None,
            path=list(page_ref.path),
            path_ids=list(page_ref.path_ids),
            blocks=blocks,
            text=text,
            metadata=PageMetadata(
                created_at=page_ref.created_at,
                modified_at=page_ref.modified_at,
                created_by_app_id=page_ref.created_by_app_id,
                level=page_ref.level,
                order=page_ref.order,
                parent_page_id=page_ref.parent_page_id,
                web_url=page_ref.web_url,
                client_url=page_ref.client_url,
                content_url=page_ref.content_url,
                fetched_at=utcnow(),
                api_version=self.config.graph.api_version,
            ),
            flags=summarize_flags(blocks, text_truncated=truncated),
            warnings=list(parsed.warnings),
        )
        page.content_hash = compute_content_hash(blocks, title=title, path=page.path)
        return page

    # -- stage 3: full run ------------------------------------------------ #

    def run(self, *, export: bool = True) -> ParseResult:
        """Execute the whole pipeline and return everything it produced."""
        run_id = uuid.uuid4().hex[:16]
        stats = RunStats(started_at=utcnow())
        logger.info("Starting run %s (mode=%s)", run_id, self.config.state.mode)

        if self.config.state.enabled:
            self.state.load()

        tree = self.discover()
        counts = tree.counts()
        stats.notebooks = counts["notebooks"]
        stats.section_groups = counts["section_groups"]
        stats.sections = counts["sections"]

        page_refs = list(tree.iter_pages())
        stats.pages_discovered = len(page_refs)
        limit = self.config.filters.max_pages
        if limit is not None and len(page_refs) > limit:
            logger.info("Limiting run to the first %d of %d page(s)", limit, len(page_refs))
            page_refs = page_refs[:limit]

        changes = ChangeSet()
        pages: list[ParsedPage] = []

        if self.config.runtime.dry_run:
            logger.info("Dry run: discovered %d page(s); no content fetched", len(page_refs))
            for ref in page_refs:
                self.state.mark_seen(ref.id)
        else:
            pages, run_changes = self._process_pages(page_refs, run_id, stats)
            for change in run_changes:
                changes.add(change)

        if self._deletion_detection_is_safe(limit):
            for change in self.state.detect_deletions():
                changes.add(change)
        elif self.config.state.detect_deletions:
            logger.info("Skipping deletion detection: this run used filters or a page limit, "
                        "so unvisited pages are not necessarily deleted")

        graph = self.client.stats
        stats.graph_requests = graph.requests
        stats.graph_retries = graph.retries
        stats.graph_throttled = graph.throttled
        stats.bytes_downloaded = graph.bytes_downloaded
        stats.images_downloaded = self.images.downloaded
        stats.images_ocr = self.images.ocr_count
        stats.attachments_downloaded = self.attachments.downloaded
        stats.attachments_extracted = self.attachments.extracted
        stats.pages_parsed = len(pages)
        stats.finished_at = utcnow()

        result = ParseResult(
            tree=tree, pages=pages, changes=changes, stats=stats,
            errors=list(self.errors), run_id=run_id,
        )

        if export and self.exporters:
            self.export(result)
        if self.config.state.enabled and not self.config.runtime.dry_run:
            self.state.save(run_id=run_id)
        self.image_store.save_index()
        self.attachment_store.save_index()
        if self.ocr_cache:
            self.ocr_cache.save()

        logger.info(
            "Run %s finished in %.1fs: %d parsed, %d skipped, %d failed, changes=%s",
            run_id, stats.duration_seconds or 0.0, stats.pages_parsed,
            stats.pages_skipped, stats.pages_failed, changes.counts(),
        )
        return result

    # -- stage 4: export --------------------------------------------------- #

    def export(self, result: ParseResult) -> dict[str, ExportManifest]:
        """Write ``result`` in every configured format."""
        output_dir = self.config.output_path
        manifests: dict[str, ExportManifest] = {}
        for exporter in self.exporters:
            manifests[exporter.name] = exporter.export(result, output_dir)
        return manifests

    # -- internals -------------------------------------------------------- #

    def _deletion_detection_is_safe(self, limit: Optional[int]) -> bool:
        """Deletion detection is only valid after an unfiltered, complete crawl."""
        if not (self.config.state.enabled and self.config.state.detect_deletions):
            return False
        filters = self.config.filters
        narrowed = any(
            (
                filters.notebook_ids, filters.notebook_names, filters.section_ids,
                filters.section_names, filters.page_ids, filters.exclude_notebook_names,
                filters.exclude_section_names, filters.modified_since,
            )
        )
        return not narrowed and limit is None and not self.config.runtime.dry_run

    def _process_pages(
        self, page_refs: Sequence[PageRef], run_id: str, stats: RunStats
    ) -> tuple[list[ParsedPage], list[Any]]:
        results: dict[str, ParsedPage] = {}
        changes: dict[str, Any] = {}
        consecutive_failures = 0

        def handle(ref: PageRef) -> tuple[str, Optional[ParsedPage], Any]:
            fetch, presumed = self.state.should_fetch(ref)
            if not fetch:
                return ref.id, None, self.state.record_skipped(ref, run_id=run_id)
            page = self.parse_page(ref)
            return ref.id, page, self.state.record_parsed(page, run_id=run_id)

        workers = max(1, self.config.runtime.concurrency)
        if workers == 1:
            iterator: Iterable[tuple[PageRef, Any]] = (
                (ref, _Deferred(handle, ref)) for ref in page_refs
            )
            for ref, deferred in iterator:
                outcome = self._settle(ref, deferred, stats)
                if outcome is None:
                    consecutive_failures += 1
                    if self._should_abort(consecutive_failures):
                        break
                    continue
                consecutive_failures = 0
                page_id, page, change = outcome
                if page is not None:
                    results[page_id] = page
                changes[page_id] = change
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="onenote-page"
            ) as pool:
                futures = {pool.submit(handle, ref): ref for ref in page_refs}
                for future in concurrent.futures.as_completed(futures):
                    ref = futures[future]
                    outcome = self._settle(ref, future, stats)
                    if outcome is None:
                        consecutive_failures += 1
                        continue
                    consecutive_failures = 0
                    page_id, page, change = outcome
                    if page is not None:
                        results[page_id] = page
                    changes[page_id] = change

        # Restore discovery order, which threads do not preserve.
        ordered_pages = [results[ref.id] for ref in page_refs if ref.id in results]
        ordered_changes = [changes[ref.id] for ref in page_refs if ref.id in changes]
        stats.pages_skipped = sum(
            1 for change in ordered_changes
            if change.status is ChangeStatus.UNCHANGED and change.reason ==
            "not_modified_since_last_run"
        )
        return ordered_pages, ordered_changes

    def _settle(self, ref: PageRef, future: Any, stats: RunStats):  # type: ignore[no-untyped-def]
        """Resolve one page's outcome, converting failures into recorded errors."""
        try:
            return future.result()
        except (AccessDeniedError, NotFoundError) as exc:
            status = "inaccessible" if isinstance(exc, AccessDeniedError) else "not_found"
            logger.warning("Page %r is %s: %s", ref.title or ref.id, status, exc)
            self._record_error(ref, status, str(exc))
            change = self.state.record_inaccessible(ref, status)
            stats.pages_failed += 1
            return (ref.id, None, change)
        except (GraphHTTPError, ParseError, OneNoteParserError) as exc:
            stats.pages_failed += 1
            self._record_error(ref, "error", str(exc))
            logger.error("Failed to parse page %r: %s", ref.title or ref.id, exc)
            if not self.config.runtime.continue_on_error:
                raise
            return None
        except Exception as exc:  # noqa: BLE001 - never let one page kill a run
            stats.pages_failed += 1
            self._record_error(ref, "unexpected_error", repr(exc))
            logger.exception("Unexpected failure on page %r", ref.title or ref.id)
            if not self.config.runtime.continue_on_error:
                raise
            return None

    def _should_abort(self, consecutive_failures: int) -> bool:
        limit = self.config.runtime.fail_fast_after
        if limit and consecutive_failures >= limit:
            logger.error("Aborting run after %d consecutive page failures", consecutive_failures)
            return True
        return False

    def _record_error(self, ref: PageRef, kind: str, message: str) -> None:
        self.errors.append(
            {
                "stage": "page",
                "kind": kind,
                "id": ref.id,
                "title": ref.title,
                "path": list(ref.path),
                "message": message,
            }
        )

    def _process_resources(self, blocks: Sequence[Any], page_ref: PageRef) -> None:
        for block in _iter_resource_blocks(blocks):
            if isinstance(block, ImageBlock):
                self.images.process(block)
            elif isinstance(block, FileBlock):
                self.attachments.process(block)

    def _store_ink(
        self, parts: Sequence[tuple[str, bytes]], page_ref: PageRef, *, start_order: int
    ) -> list[FileBlock]:
        """Persist InkML parts so handwriting is preserved, not just flagged."""
        blocks: list[FileBlock] = []
        for index, (content_type, payload) in enumerate(parts, start=1):
            name = f"{page_ref.id[:24]}-ink-{index}.inkml"
            try:
                path, digest, _ = self.attachment_store.store(
                    payload, filename=name, mime_type=content_type or "application/inkml+xml"
                )
            except OneNoteParserError as exc:
                logger.warning("Could not store InkML for page %s: %s", page_ref.id, exc)
                continue
            blocks.append(
                FileBlock(
                    order=start_order + index - 1,
                    file_name=name,
                    mime_type=content_type or "application/inkml+xml",
                    file_path=self.attachment_store.relative(
                        path, base=self.config.output_path
                    ),
                    sha256=digest,
                    byte_size=len(payload),
                    downloaded=True,
                    extraction_status="unsupported",
                )
            )
        return blocks


class _Deferred:
    """Minimal future-like wrapper so the serial path shares the error handling."""

    __slots__ = ("_fn", "_arg")

    def __init__(self, fn: Callable[..., Any], arg: Any) -> None:
        self._fn = fn
        self._arg = arg

    def result(self) -> Any:
        return self._fn(self._arg)


def _iter_resource_blocks(blocks: Iterable[Any]) -> Iterable[Any]:
    from .models import ListBlock, TableBlock

    for block in blocks:
        if isinstance(block, (ImageBlock, FileBlock)):
            yield block
        elif isinstance(block, ListBlock):
            for item in block.items:
                yield from _iter_resource_blocks(item.blocks)
        elif isinstance(block, TableBlock):
            for row in block.rows:
                for cell in row.cells:
                    yield from _iter_resource_blocks(cell.blocks)
