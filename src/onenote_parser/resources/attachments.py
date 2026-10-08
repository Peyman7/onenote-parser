"""Download and text extraction for embedded files and attachments.

Extractors are registered in a table keyed by extension and MIME type, so a host
can add a format (``register_extractor``) without touching the pipeline.  The
Office formats are handled by reading the OOXML zip directly, which keeps the
common cases dependency-free; PDF uses ``pypdf`` when installed.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from ..config import AttachmentConfig
from ..errors import ResourceError
from ..logging_utils import get_logger
from ..models import FileBlock
from .store import ResourceStore, extension_for_mime, sha256_hex

logger = get_logger("resources.attachments")

__all__ = [
    "TextExtractor",
    "AttachmentProcessor",
    "register_extractor",
    "extractor_for",
    "PlainTextExtractor",
    "HtmlTextExtractor",
    "PdfTextExtractor",
    "DocxTextExtractor",
    "PptxTextExtractor",
    "XlsxTextExtractor",
]


@runtime_checkable
class TextExtractor(Protocol):
    """Turns attachment bytes into plain text."""

    name: str
    extensions: tuple[str, ...]
    mime_types: tuple[str, ...]

    def extract(self, data: bytes, *, filename: Optional[str] = None) -> str:
        ...


_REGISTRY: list[TextExtractor] = []


def register_extractor(extractor: TextExtractor, *, prepend: bool = True) -> None:
    """Add a custom extractor.  Later registrations win by default."""
    if prepend:
        _REGISTRY.insert(0, extractor)
    else:
        _REGISTRY.append(extractor)


def extractor_for(filename: Optional[str], mime_type: Optional[str]) -> Optional[TextExtractor]:
    """Best extractor for a file, matched on extension first, then MIME type."""
    suffix = Path(filename or "").suffix.lower()
    mime = (mime_type or "").split(";")[0].strip().lower()
    if suffix:
        for extractor in _REGISTRY:
            if suffix in extractor.extensions:
                return extractor
    if mime:
        for extractor in _REGISTRY:
            if mime in extractor.mime_types:
                return extractor
    return None


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class PlainTextExtractor:
    name = "plaintext"
    extensions = (".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".xml",
                  ".yaml", ".yml", ".ini", ".cfg", ".conf", ".rst", ".sql", ".py", ".js",
                  ".ts", ".java", ".cs", ".c", ".h", ".cpp", ".sh", ".ps1")
    mime_types = ("text/plain", "text/csv", "text/markdown", "application/json",
                  "application/xml", "text/xml", "text/tab-separated-values")

    def extract(self, data: bytes, *, filename: Optional[str] = None) -> str:
        text = _decode(data)
        if (filename or "").lower().endswith(".json"):
            try:
                return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                return text
        return text


class HtmlTextExtractor:
    name = "html"
    extensions = (".html", ".htm", ".xhtml")
    mime_types = ("text/html", "application/xhtml+xml")

    def extract(self, data: bytes, *, filename: Optional[str] = None) -> str:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(_decode(data), "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return soup.get_text("\n", strip=True)


#: Annotation subtypes whose text is navigation, not content.
_SKIP_ANNOTATIONS = {"/Link", "/Popup"}


def _annotation_text(page: Any) -> list[str]:
    """Text carried by a page's annotations, in document order.

    Markup tools (Bluebeam, Acrobat) store FreeText callouts, stamps and form
    values as annotations rather than page content, so a heavily marked-up
    drawing or checklist can have a completely empty content stream while
    holding all of its text here.  ``/RC`` is skipped: it is the rich-text
    encoding of the same string already present in ``/Contents``.
    """
    annotations = page.get("/Annots")
    if annotations is None:
        return []
    if hasattr(annotations, "get_object"):
        annotations = annotations.get_object()
    found: list[str] = []
    for reference in annotations or []:
        try:
            annotation = reference.get_object()
        except Exception:  # noqa: BLE001 - a damaged annotation must not stop the page
            continue
        if str(annotation.get("/Subtype") or "") in _SKIP_ANNOTATIONS:
            continue
        for key in ("/Contents", "/V"):
            value = annotation.get(key)
            if value is None:
                continue
            if hasattr(value, "get_object"):
                value = value.get_object()
            # Annotation contents use CR as their line separator.
            text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
            if text:
                found.append(text)
    return found


class PdfTextExtractor:
    """Page text, plus annotation text when the page stream has none.

    Set ``attachments.pdf_include_annotations=False`` to read only the page
    content stream.
    """

    name = "pdf"
    extensions = (".pdf",)
    mime_types = ("application/pdf",)

    def __init__(self, *, include_annotations: bool = True) -> None:
        self.include_annotations = include_annotations

    def extract(self, data: bytes, *, filename: Optional[str] = None) -> str:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ResourceError(
                "PDF extraction requires pypdf: pip install 'onenote-parser[pdf]'"
            ) from exc
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001
                raise ResourceError(f"Encrypted PDF could not be opened: {exc}") from exc
        pages: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception as exc:  # noqa: BLE001 - malformed page must not abort
                logger.warning("PDF page %d extraction failed: %s", index, exc)
                text = ""

            if self.include_annotations:
                try:
                    extras = _annotation_text(page)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("PDF page %d annotation read failed: %s", index, exc)
                    extras = []
                # Only add what the content stream did not already provide, so
                # a normal PDF with a few comments does not get duplicated text.
                seen: set[str] = set()
                additions = []
                for value in extras:
                    if value in seen or value in text:
                        continue
                    seen.add(value)
                    additions.append(value)
                if additions:
                    text = "\n".join(filter(None, [text, *additions]))
            pages.append(text)
        return "\n\n".join(p for p in pages if p.strip())


_XML_TEXT = re.compile(r"<(?:w|a):t(?:\s[^>]*)?>(.*?)</(?:w|a):t>", re.DOTALL)
_XML_BREAK = re.compile(r"<(?:w:p|w:br|a:p|a:br)\b[^>]*/?>")
_XML_TAG = re.compile(r"<[^>]+>")


def _ooxml_text(xml: str) -> str:
    """Extract visible text from a WordprocessingML / DrawingML part."""
    from html import unescape

    marked = _XML_BREAK.sub("\n", xml)
    chunks = _XML_TEXT.findall(marked)
    if chunks:
        # Re-walk in document order so paragraph breaks are kept.
        parts: list[str] = []
        for segment in marked.split("\n"):
            texts = _XML_TEXT.findall(segment)
            if texts:
                parts.append(unescape(_XML_TAG.sub("", "".join(texts))))
        return "\n".join(p for p in parts if p.strip())
    return unescape(_XML_TAG.sub(" ", marked)).strip()


class DocxTextExtractor:
    """Reads ``word/document.xml`` straight out of the OOXML package."""

    name = "docx"
    extensions = (".docx",)
    mime_types = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",)

    def extract(self, data: bytes, *, filename: Optional[str] = None) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                parts = ["word/document.xml"]
                parts += sorted(
                    n for n in archive.namelist()
                    if n.startswith(("word/header", "word/footer")) and n.endswith(".xml")
                )
                out: list[str] = []
                for part in parts:
                    if part in archive.namelist():
                        out.append(_ooxml_text(archive.read(part).decode("utf-8", "replace")))
                return "\n\n".join(p for p in out if p.strip())
        except zipfile.BadZipFile as exc:
            raise ResourceError(f"Not a valid .docx package: {exc}") from exc


class PptxTextExtractor:
    name = "pptx"
    extensions = (".pptx",)
    mime_types = ("application/vnd.openxmlformats-officedocument.presentationml.presentation",)

    def extract(self, data: bytes, *, filename: Optional[str] = None) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                slides = sorted(
                    (n for n in archive.namelist()
                     if n.startswith("ppt/slides/slide") and n.endswith(".xml")),
                    key=lambda n: int(re.sub(r"\D", "", n) or 0),
                )
                out = []
                for index, slide in enumerate(slides, start=1):
                    text = _ooxml_text(archive.read(slide).decode("utf-8", "replace"))
                    if text.strip():
                        out.append(f"## Slide {index}\n{text}")
                return "\n\n".join(out)
        except zipfile.BadZipFile as exc:
            raise ResourceError(f"Not a valid .pptx package: {exc}") from exc


class XlsxTextExtractor:
    """Uses ``openpyxl`` when available; otherwise falls back to shared strings."""

    name = "xlsx"
    extensions = (".xlsx", ".xlsm")
    mime_types = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",)

    def extract(self, data: bytes, *, filename: Optional[str] = None) -> str:
        try:
            import openpyxl  # type: ignore

            workbook = openpyxl.load_workbook(
                io.BytesIO(data), read_only=True, data_only=True
            )
            out: list[str] = []
            for sheet in workbook.worksheets:
                rows = []
                for row in sheet.iter_rows(values_only=True):
                    cells = ["" if cell is None else str(cell) for cell in row]
                    if any(c.strip() for c in cells):
                        rows.append(" | ".join(cells).rstrip(" |"))
                if rows:
                    out.append(f"## {sheet.title}\n" + "\n".join(rows))
            workbook.close()
            return "\n\n".join(out)
        except ImportError:
            logger.debug("openpyxl unavailable; falling back to shared-string extraction")
        except Exception as exc:  # noqa: BLE001
            raise ResourceError(f"Could not read workbook: {exc}") from exc

        from html import unescape

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "xl/sharedStrings.xml" not in archive.namelist():
                    return ""
                xml = archive.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                values = re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", xml, re.DOTALL)
                return "\n".join(unescape(_XML_TAG.sub("", v)) for v in values)
        except zipfile.BadZipFile as exc:
            raise ResourceError(f"Not a valid .xlsx package: {exc}") from exc


for _extractor in (
    PlainTextExtractor(), HtmlTextExtractor(), PdfTextExtractor(),
    DocxTextExtractor(), PptxTextExtractor(), XlsxTextExtractor(),
):
    register_extractor(_extractor, prepend=False)


class AttachmentProcessor:
    """Downloads attachments and, optionally, extracts their text."""

    def __init__(
        self,
        client: Any,
        config: AttachmentConfig,
        store: ResourceStore,
        *,
        output_root: Optional[Path] = None,
    ) -> None:
        self.client = client
        self.config = config
        self.store = store
        self.output_root = output_root
        self.downloaded = 0
        self.extracted = 0
        self.bytes_downloaded = 0

    def process(self, block: FileBlock) -> FileBlock:
        """Populate ``block`` in place with local path, hash and extracted text."""
        if not self.config.download or not block.source_url:
            block.extraction_status = "skipped"
            return block

        suffix = Path(block.file_name or "").suffix.lower()
        if suffix and suffix in self.config.denied_extensions:
            block.error = f"extension_denied:{suffix}"
            block.extraction_status = "skipped"
            logger.info("Skipping denied attachment %s", block.file_name)
            return block
        if self.config.allowed_extensions and suffix not in self.config.allowed_extensions:
            block.error = f"extension_not_allowed:{suffix}"
            block.extraction_status = "skipped"
            return block

        cached = self._reuse(block)
        if cached is not None:
            return cached

        from .images import _resolve_resource_url

        url = _resolve_resource_url(self.client, block.source_url)
        try:
            data, headers = self.client.get_bytes(url)
        except Exception as exc:  # noqa: BLE001 - one bad resource must not fail a page
            block.error = f"download_failed: {exc}"
            block.extraction_status = "failed"
            logger.warning("Attachment download failed (%s): %s", block.file_name, exc)
            return block

        if len(data) > self.config.max_bytes:
            block.error = f"too_large:{len(data)}"
            block.extraction_status = "skipped"
            block.byte_size = len(data)
            logger.info("Attachment %s exceeds max_bytes (%d)", block.file_name, len(data))
            return block

        block.mime_type = block.mime_type or (headers.get("content-type") or "").split(";")[0] or None
        digest = sha256_hex(data)
        name = block.file_name or f"attachment{extension_for_mime(block.mime_type)}"
        path, checksum, reused = self.store.store(
            data, filename=name, mime_type=block.mime_type, digest=digest
        )
        block.file_path = self.store.relative(path, base=self.output_root)
        block.sha256 = checksum
        block.byte_size = len(data)
        block.downloaded = True
        if not reused:
            self.downloaded += 1
            self.bytes_downloaded += len(data)

        self.store.remember(block.resource_id, {
            "file_path": block.file_path, "sha256": checksum,
            "byte_size": len(data), "mime_type": block.mime_type,
            "file_name": block.file_name,
        })
        if self.config.extract_text:
            self._extract(block, data)
        return block

    def _reuse(self, block: FileBlock) -> Optional[FileBlock]:
        """Populate the block from a previously downloaded file, if available."""
        if not self.config.reuse_downloaded:
            return None
        entry = self.store.lookup(block.resource_id)
        if entry is None:
            return None
        path = self.store.root.parent / entry["file_path"]
        block.file_path = entry["file_path"]
        block.sha256 = entry.get("sha256")
        block.byte_size = entry.get("byte_size")
        block.mime_type = entry.get("mime_type") or block.mime_type
        block.downloaded = True
        logger.debug("Reusing cached attachment %s", block.resource_id)
        if self.config.extract_text:
            try:
                self._extract(block, path.read_bytes())
            except OSError as exc:  # pragma: no cover - file vanished
                logger.warning("Could not read cached attachment %s: %s", path, exc)
                block.extraction_status = "failed"
        return block

    def _extract(self, block: FileBlock, data: bytes) -> None:
        extractor = extractor_for(block.file_name, block.mime_type)
        if isinstance(extractor, PdfTextExtractor):
            extractor = PdfTextExtractor(
                include_annotations=self.config.pdf_include_annotations
            )
        if extractor is None:
            block.extraction_status = "unsupported"
            return
        try:
            text = extractor.extract(data, filename=block.file_name) or ""
        except ResourceError as exc:
            block.extraction_status = "failed"
            block.error = str(exc)
            logger.warning("Text extraction failed for %s: %s", block.file_name, exc)
            return
        except Exception as exc:  # noqa: BLE001
            block.extraction_status = "failed"
            block.error = f"extraction_error: {exc}"
            logger.warning("Text extraction crashed for %s: %s", block.file_name, exc)
            return

        text = text.strip()
        block.extractor = extractor.name
        if not text:
            block.extraction_status = "empty"
            return
        limit = self.config.max_extracted_chars
        if limit and len(text) > limit:
            text = text[:limit].rstrip()
            block.extraction_status = "truncated"
        else:
            block.extraction_status = "ok"
        block.extracted_text = text
        self.extracted += 1
