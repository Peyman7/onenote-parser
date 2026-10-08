"""Normalized domain model produced by the parser.

This module is the public data contract.  Everything the parser emits -- JSON,
JSONL, Markdown front-matter -- is a serialization of the dataclasses below, and
:data:`SCHEMA_VERSION` is stamped on every page and run so downstream consumers
(RAG, search, migration, analytics) can pin behaviour.

Design rules
------------
* Dataclasses only, no third-party model library, so the contract is importable
  anywhere and cheap to copy into another codebase.
* Every object exposes ``to_dict()`` returning plain JSON-safe primitives, and
  container objects expose ``from_dict()`` for round-tripping.
* Ordering is explicit: :attr:`Block.order` is the 0-based position of the block
  in the page, and blocks are always stored in document order.
* Nothing is silently dropped.  Content the parser cannot classify is preserved
  as :class:`UnsupportedBlock` with the original HTML.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator, Mapping, Optional

SCHEMA_VERSION = "1.0"
"""Version of the normalized output schema.  Bumped on breaking changes."""

__all__ = [
    "SCHEMA_VERSION",
    "BlockType",
    "MarkType",
    "LinkKind",
    "ChangeStatus",
    "Mark",
    "RichText",
    "Block",
    "HeadingBlock",
    "ParagraphBlock",
    "ListItem",
    "ListBlock",
    "TableCell",
    "TableRow",
    "TableBlock",
    "ImageBlock",
    "FileBlock",
    "CodeBlock",
    "QuoteBlock",
    "MathBlock",
    "DividerBlock",
    "UnsupportedBlock",
    "OcrResult",
    "PageFlags",
    "PageMetadata",
    "PageRef",
    "SectionNode",
    "SectionGroupNode",
    "NotebookNode",
    "NotebookTree",
    "ParsedPage",
    "PageChange",
    "ChangeSet",
    "RunStats",
    "ParseResult",
    "block_from_dict",
    "utcnow",
    "parse_graph_datetime",
]


def utcnow() -> datetime:
    """Timezone-aware current UTC time (tests monkeypatch this)."""
    return datetime.now(timezone.utc)


def parse_graph_datetime(value: Any) -> Optional[datetime]:
    """Parse a Microsoft Graph ISO-8601 timestamp into an aware datetime.

    Graph returns e.g. ``2024-03-01T09:12:33.7Z``.  Returns ``None`` for empty
    or unparseable input rather than raising, because a bad timestamp must not
    abort a crawl.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class BlockType(str, Enum):
    """Discriminator stored in the serialized ``type`` field of every block."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    IMAGE = "image"
    FILE = "file"
    CODE = "code"
    QUOTE = "quote"
    MATH = "math"
    DIVIDER = "divider"
    UNSUPPORTED = "unsupported"


class MarkType(str, Enum):
    """Inline annotation kinds applied to a :class:`RichText` range."""

    BOLD = "bold"
    ITALIC = "italic"
    UNDERLINE = "underline"
    STRIKETHROUGH = "strikethrough"
    CODE = "code"
    SUPERSCRIPT = "superscript"
    SUBSCRIPT = "subscript"
    HIGHLIGHT = "highlight"
    LINK = "link"


class LinkKind(str, Enum):
    """Classification of a hyperlink target."""

    EXTERNAL = "external"
    ONENOTE = "onenote"
    MAILTO = "mailto"
    ANCHOR = "anchor"
    FILE = "file"
    UNKNOWN = "unknown"


class ChangeStatus(str, Enum):
    """Result of comparing a page against the previous run's state."""

    NEW = "new"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"
    DELETED = "deleted"
    INACCESSIBLE = "inaccessible"


# --------------------------------------------------------------------------- #
# Rich text
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Mark:
    """An inline annotation over ``text[start:end]`` of a :class:`RichText`."""

    start: int
    end: int
    type: MarkType
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"start": self.start, "end": self.end, "type": self.type.value}
        if self.attrs:
            out["attrs"] = dict(self.attrs)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Mark":
        return cls(
            start=int(data["start"]),
            end=int(data["end"]),
            type=MarkType(data["type"]),
            attrs=dict(data.get("attrs") or {}),
        )


@dataclass(slots=True)
class RichText:
    """Plain text plus non-destructive inline annotations.

    Formatting is stored as offset ranges rather than nested nodes so that the
    ``text`` field is directly usable by chunkers, search indexers and diff
    tools, while styling and links remain recoverable.
    """

    text: str = ""
    marks: list[Mark] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.text.strip())

    @property
    def links(self) -> list[dict[str, Any]]:
        """All link marks as ``{text, href, kind, ...}`` dicts."""
        out = []
        for mark in self.marks:
            if mark.type is MarkType.LINK:
                item = dict(mark.attrs)
                item["text"] = self.text[mark.start : mark.end]
                out.append(item)
        return out

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"text": self.text}
        if self.marks:
            out["marks"] = [m.to_dict() for m in self.marks]
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | str | None) -> "RichText":
        if data is None:
            return cls()
        if isinstance(data, str):
            return cls(text=data)
        return cls(
            text=data.get("text", ""),
            marks=[Mark.from_dict(m) for m in data.get("marks") or []],
        )


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Block:
    """Base class for content blocks.

    ``source_id`` carries the OneNote element id returned when the page HTML is
    fetched with ``includeIDs=true``; it is the stable anchor used to build deep
    links back into the page and to diff blocks between runs.
    """

    type: BlockType = field(init=False, default=BlockType.PARAGRAPH)
    order: int = 0
    source_id: Optional[str] = None
    #: OneNote note tag on the element (``to-do``, ``important``, ``question``...).
    data_tag: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type.value, "order": self.order}
        if self.source_id:
            out["source_id"] = self.source_id
        if self.data_tag:
            out["data_tag"] = self.data_tag
        out.update(self._payload())
        return out

    def _payload(self) -> dict[str, Any]:  # pragma: no cover - overridden
        return {}

    def text_content(self) -> str:
        """Plain-text rendering used for hashing, search and previews."""
        return ""


@dataclass(slots=True)
class HeadingBlock(Block):
    level: int = 1
    content: RichText = field(default_factory=RichText)

    def __post_init__(self) -> None:
        self.type = BlockType.HEADING
        self.level = max(1, min(6, int(self.level)))

    def _payload(self) -> dict[str, Any]:
        return {"level": self.level, "content": self.content.to_dict()}

    def text_content(self) -> str:
        return self.content.text


@dataclass(slots=True)
class ParagraphBlock(Block):
    content: RichText = field(default_factory=RichText)

    def __post_init__(self) -> None:
        self.type = BlockType.PARAGRAPH

    def _payload(self) -> dict[str, Any]:
        return {"content": self.content.to_dict()}

    def text_content(self) -> str:
        return self.content.text


@dataclass(slots=True)
class ListItem:
    """One entry of a :class:`ListBlock`.

    Nesting is expressed by :attr:`level` (0-based) rather than recursion, which
    keeps the JSON flat and stable while remaining loss-free.
    """

    content: RichText = field(default_factory=RichText)
    level: int = 0
    checked: Optional[bool] = None
    source_id: Optional[str] = None
    #: Overrides the parent block's ``ordered`` flag for mixed nested lists.
    ordered: Optional[bool] = None
    data_tag: Optional[str] = None
    blocks: list[Block] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"content": self.content.to_dict(), "level": self.level}
        if self.checked is not None:
            out["checked"] = self.checked
        if self.ordered is not None:
            out["ordered"] = self.ordered
        if self.data_tag:
            out["data_tag"] = self.data_tag
        if self.source_id:
            out["source_id"] = self.source_id
        if self.blocks:
            out["blocks"] = [b.to_dict() for b in self.blocks]
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ListItem":
        return cls(
            content=RichText.from_dict(data.get("content")),
            level=int(data.get("level", 0)),
            checked=data.get("checked"),
            source_id=data.get("source_id"),
            ordered=data.get("ordered"),
            data_tag=data.get("data_tag"),
            blocks=[block_from_dict(b) for b in data.get("blocks") or []],
        )


@dataclass(slots=True)
class ListBlock(Block):
    ordered: bool = False
    items: list[ListItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.type = BlockType.LIST

    def _payload(self) -> dict[str, Any]:
        return {"ordered": self.ordered, "items": [i.to_dict() for i in self.items]}

    def text_content(self) -> str:
        lines = []
        for index, item in enumerate(self.items, start=1):
            ordered = self.ordered if item.ordered is None else item.ordered
            bullet = f"{index}." if ordered else "-"
            lines.append(f"{'  ' * item.level}{bullet} {item.content.text}")
        return "\n".join(lines)


@dataclass(slots=True)
class TableCell:
    content: RichText = field(default_factory=RichText)
    colspan: int = 1
    rowspan: int = 1
    header: bool = False
    blocks: list[Block] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"content": self.content.to_dict()}
        if self.colspan != 1:
            out["colspan"] = self.colspan
        if self.rowspan != 1:
            out["rowspan"] = self.rowspan
        if self.header:
            out["header"] = True
        if self.blocks:
            out["blocks"] = [b.to_dict() for b in self.blocks]
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TableCell":
        return cls(
            content=RichText.from_dict(data.get("content")),
            colspan=int(data.get("colspan", 1)),
            rowspan=int(data.get("rowspan", 1)),
            header=bool(data.get("header", False)),
            blocks=[block_from_dict(b) for b in data.get("blocks") or []],
        )


@dataclass(slots=True)
class TableRow:
    cells: list[TableCell] = field(default_factory=list)
    header: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"cells": [c.to_dict() for c in self.cells]}
        if self.header:
            out["header"] = True
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TableRow":
        return cls(
            cells=[TableCell.from_dict(c) for c in data.get("cells") or []],
            header=bool(data.get("header", False)),
        )


@dataclass(slots=True)
class TableBlock(Block):
    rows: list[TableRow] = field(default_factory=list)
    has_header: bool = False
    borders: bool = True

    def __post_init__(self) -> None:
        self.type = BlockType.TABLE

    @property
    def column_count(self) -> int:
        return max((sum(c.colspan for c in row.cells) for row in self.rows), default=0)

    def _payload(self) -> dict[str, Any]:
        return {
            "has_header": self.has_header,
            "borders": self.borders,
            "column_count": self.column_count,
            "rows": [r.to_dict() for r in self.rows],
        }

    def to_markdown(self) -> str:
        """Render as a GitHub-flavoured Markdown table (pipes escaped)."""
        if not self.rows:
            return ""
        width = self.column_count or max(len(r.cells) for r in self.rows)

        def render_row(row: TableRow) -> str:
            cells = [c.content.text.replace("|", "\\|").replace("\n", "<br>") for c in row.cells]
            cells += [""] * (width - len(cells))
            return "| " + " | ".join(cells) + " |"

        lines = []
        start = 0
        if self.has_header and self.rows:
            lines.append(render_row(self.rows[0]))
            lines.append("|" + "|".join([" --- "] * width) + "|")
            start = 1
        else:
            lines.append("|" + "|".join([" " * 3] * width) + "|")
            lines.append("|" + "|".join([" --- "] * width) + "|")
        for row in self.rows[start:]:
            lines.append(render_row(row))
        return "\n".join(lines)

    def text_content(self) -> str:
        return self.to_markdown()


@dataclass(slots=True)
class OcrResult:
    """Text recovered from an image by an OCR engine."""

    text: str = ""
    engine: str = ""
    confidence: Optional[float] = None
    language: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v not in (None, "")}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OcrResult":
        return cls(**{k: v for k, v in data.items() if k in {f.name for f in dataclasses.fields(cls)}})


@dataclass(slots=True)
class ImageBlock(Block):
    """An image, screenshot, scan or rendered ink stroke on a page."""

    resource_id: Optional[str] = None
    source_url: Optional[str] = None
    full_resource_url: Optional[str] = None
    alt_text: Optional[str] = None
    width: Optional[float] = None
    height: Optional[float] = None
    mime_type: Optional[str] = None
    file_path: Optional[str] = None
    sha256: Optional[str] = None
    byte_size: Optional[int] = None
    is_ink: bool = False
    downloaded: bool = False
    ocr: Optional[OcrResult] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        self.type = BlockType.IMAGE

    def _payload(self) -> dict[str, Any]:
        payload = {
            "resource_id": self.resource_id,
            "source_url": self.source_url,
            "full_resource_url": self.full_resource_url,
            "alt_text": self.alt_text,
            "width": self.width,
            "height": self.height,
            "mime_type": self.mime_type,
            "file_path": self.file_path,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "is_ink": self.is_ink,
            "downloaded": self.downloaded,
            "error": self.error,
        }
        if self.ocr is not None:
            payload["ocr"] = self.ocr.to_dict()
        return {k: v for k, v in payload.items() if v is not None}

    def text_content(self) -> str:
        parts = []
        if self.alt_text:
            parts.append(self.alt_text)
        if self.ocr and self.ocr.text:
            parts.append(self.ocr.text)
        return "\n".join(parts)


@dataclass(slots=True)
class FileBlock(Block):
    """An embedded file or attachment (``<object data-attachment>``)."""

    resource_id: Optional[str] = None
    file_name: Optional[str] = None
    source_url: Optional[str] = None
    mime_type: Optional[str] = None
    file_path: Optional[str] = None
    sha256: Optional[str] = None
    byte_size: Optional[int] = None
    downloaded: bool = False
    extracted_text: Optional[str] = None
    extraction_status: str = "not_attempted"
    extractor: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        self.type = BlockType.FILE

    def _payload(self) -> dict[str, Any]:
        payload = {
            "resource_id": self.resource_id,
            "file_name": self.file_name,
            "source_url": self.source_url,
            "mime_type": self.mime_type,
            "file_path": self.file_path,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "downloaded": self.downloaded,
            "extracted_text": self.extracted_text,
            "extraction_status": self.extraction_status,
            "extractor": self.extractor,
            "error": self.error,
        }
        return {k: v for k, v in payload.items() if v is not None}

    def text_content(self) -> str:
        parts = []
        if self.file_name:
            parts.append(f"[attachment: {self.file_name}]")
        if self.extracted_text:
            parts.append(self.extracted_text)
        return "\n".join(parts)


@dataclass(slots=True)
class CodeBlock(Block):
    code: str = ""
    language: Optional[str] = None

    def __post_init__(self) -> None:
        self.type = BlockType.CODE

    def _payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code}
        if self.language:
            out["language"] = self.language
        return out

    def text_content(self) -> str:
        return self.code


@dataclass(slots=True)
class QuoteBlock(Block):
    content: RichText = field(default_factory=RichText)

    def __post_init__(self) -> None:
        self.type = BlockType.QUOTE

    def _payload(self) -> dict[str, Any]:
        return {"content": self.content.to_dict()}

    def text_content(self) -> str:
        return self.content.text


@dataclass(slots=True)
class MathBlock(Block):
    """Mathematical or technical notation (MathML, LaTeX or OMML remnants)."""

    mathml: Optional[str] = None
    latex: Optional[str] = None
    text: str = ""
    inline: bool = False
    notation: str = "unknown"

    def __post_init__(self) -> None:
        self.type = BlockType.MATH

    def _payload(self) -> dict[str, Any]:
        payload = {
            "mathml": self.mathml,
            "latex": self.latex,
            "text": self.text,
            "inline": self.inline,
            "notation": self.notation,
        }
        return {k: v for k, v in payload.items() if v is not None}

    def text_content(self) -> str:
        return self.latex or self.text


@dataclass(slots=True)
class DividerBlock(Block):
    def __post_init__(self) -> None:
        self.type = BlockType.DIVIDER


@dataclass(slots=True)
class UnsupportedBlock(Block):
    """Preserved verbatim markup the parser deliberately did not interpret.

    Keeping the original HTML makes the export loss-free and lets downstream
    users handle custom OneNote add-in markup without re-crawling.
    """

    tag: str = ""
    html: str = ""
    text: str = ""
    reason: str = "unrecognized_element"

    def __post_init__(self) -> None:
        self.type = BlockType.UNSUPPORTED

    def _payload(self) -> dict[str, Any]:
        return {"tag": self.tag, "html": self.html, "text": self.text, "reason": self.reason}

    def text_content(self) -> str:
        return self.text


_BLOCK_CLASSES: dict[str, type[Block]] = {
    BlockType.HEADING.value: HeadingBlock,
    BlockType.PARAGRAPH.value: ParagraphBlock,
    BlockType.LIST.value: ListBlock,
    BlockType.TABLE.value: TableBlock,
    BlockType.IMAGE.value: ImageBlock,
    BlockType.FILE.value: FileBlock,
    BlockType.CODE.value: CodeBlock,
    BlockType.QUOTE.value: QuoteBlock,
    BlockType.MATH.value: MathBlock,
    BlockType.DIVIDER.value: DividerBlock,
    BlockType.UNSUPPORTED.value: UnsupportedBlock,
}


def block_from_dict(data: Mapping[str, Any]) -> Block:
    """Rebuild a block from its serialized form (inverse of ``to_dict``)."""
    kind = data.get("type")
    cls = _BLOCK_CLASSES.get(str(kind))
    if cls is None:
        return UnsupportedBlock(
            order=int(data.get("order", 0)),
            source_id=data.get("source_id"),
            tag=str(kind),
            html=str(data.get("html", "")),
            text=str(data.get("text", "")),
            reason="unknown_block_type",
        )
    common = {
        "order": int(data.get("order", 0)),
        "source_id": data.get("source_id"),
        "data_tag": data.get("data_tag"),
    }
    if cls is HeadingBlock:
        return HeadingBlock(level=int(data.get("level", 1)),
                            content=RichText.from_dict(data.get("content")), **common)
    if cls is ParagraphBlock:
        return ParagraphBlock(content=RichText.from_dict(data.get("content")), **common)
    if cls is QuoteBlock:
        return QuoteBlock(content=RichText.from_dict(data.get("content")), **common)
    if cls is ListBlock:
        return ListBlock(
            ordered=bool(data.get("ordered", False)),
            items=[ListItem.from_dict(i) for i in data.get("items") or []],
            **common,
        )
    if cls is TableBlock:
        return TableBlock(
            rows=[TableRow.from_dict(r) for r in data.get("rows") or []],
            has_header=bool(data.get("has_header", False)),
            borders=bool(data.get("borders", True)),
            **common,
        )
    if cls is DividerBlock:
        return DividerBlock(**common)
    if cls is CodeBlock:
        return CodeBlock(code=data.get("code", ""), language=data.get("language"), **common)
    if cls is MathBlock:
        return MathBlock(
            mathml=data.get("mathml"), latex=data.get("latex"), text=data.get("text", ""),
            inline=bool(data.get("inline", False)), notation=data.get("notation", "unknown"),
            **common,
        )
    if cls is UnsupportedBlock:
        return UnsupportedBlock(
            tag=data.get("tag", ""), html=data.get("html", ""), text=data.get("text", ""),
            reason=data.get("reason", "unrecognized_element"), **common,
        )
    field_names = {f.name for f in dataclasses.fields(cls)} - {"type", "order", "source_id"}
    kwargs = {k: v for k, v in data.items() if k in field_names}
    if cls is ImageBlock and data.get("ocr"):
        kwargs["ocr"] = OcrResult.from_dict(data["ocr"])
    return cls(**common, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Hierarchy
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PageRef:
    """A page as advertised by the section listing, before its HTML is fetched."""

    id: str
    title: str = ""
    level: int = 0
    order: int = 0
    created_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    content_url: Optional[str] = None
    web_url: Optional[str] = None
    client_url: Optional[str] = None
    parent_page_id: Optional[str] = None
    created_by_app_id: Optional[str] = None
    section_id: Optional[str] = None
    notebook_id: Optional[str] = None
    path: list[str] = field(default_factory=list)
    path_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "level": self.level,
            "order": self.order,
            "created_at": _iso(self.created_at),
            "modified_at": _iso(self.modified_at),
            "content_url": self.content_url,
            "web_url": self.web_url,
            "client_url": self.client_url,
            "parent_page_id": self.parent_page_id,
            "created_by_app_id": self.created_by_app_id,
            "section_id": self.section_id,
            "notebook_id": self.notebook_id,
            "path": list(self.path),
            "path_ids": list(self.path_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PageRef":
        return cls(
            id=data["id"],
            title=data.get("title", ""),
            level=int(data.get("level", 0)),
            order=int(data.get("order", 0)),
            created_at=parse_graph_datetime(data.get("created_at")),
            modified_at=parse_graph_datetime(data.get("modified_at")),
            content_url=data.get("content_url"),
            web_url=data.get("web_url"),
            client_url=data.get("client_url"),
            parent_page_id=data.get("parent_page_id"),
            created_by_app_id=data.get("created_by_app_id"),
            section_id=data.get("section_id"),
            notebook_id=data.get("notebook_id"),
            path=list(data.get("path") or []),
            path_ids=list(data.get("path_ids") or []),
        )


@dataclass(slots=True)
class SectionNode:
    id: str
    display_name: str = ""
    created_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    web_url: Optional[str] = None
    client_url: Optional[str] = None
    is_default: bool = False
    created_by: Optional[str] = None
    last_modified_by: Optional[str] = None
    notebook_id: Optional[str] = None
    parent_group_id: Optional[str] = None
    path: list[str] = field(default_factory=list)
    path_ids: list[str] = field(default_factory=list)
    pages: list[PageRef] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "section",
            "id": self.id,
            "display_name": self.display_name,
            "created_at": _iso(self.created_at),
            "modified_at": _iso(self.modified_at),
            "web_url": self.web_url,
            "client_url": self.client_url,
            "is_default": self.is_default,
            "created_by": self.created_by,
            "last_modified_by": self.last_modified_by,
            "notebook_id": self.notebook_id,
            "parent_group_id": self.parent_group_id,
            "path": list(self.path),
            "path_ids": list(self.path_ids),
            "page_count": len(self.pages),
            "pages": [p.to_dict() for p in self.pages],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SectionNode":
        return cls(
            id=data["id"],
            display_name=data.get("display_name", ""),
            created_at=parse_graph_datetime(data.get("created_at")),
            modified_at=parse_graph_datetime(data.get("modified_at")),
            web_url=data.get("web_url"),
            client_url=data.get("client_url"),
            is_default=bool(data.get("is_default", False)),
            created_by=data.get("created_by"),
            last_modified_by=data.get("last_modified_by"),
            notebook_id=data.get("notebook_id"),
            parent_group_id=data.get("parent_group_id"),
            path=list(data.get("path") or []),
            path_ids=list(data.get("path_ids") or []),
            pages=[PageRef.from_dict(p) for p in data.get("pages") or []],
            error=data.get("error"),
        )


@dataclass(slots=True)
class SectionGroupNode:
    """A section group, which may nest other section groups arbitrarily deep."""

    id: str
    display_name: str = ""
    created_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    web_url: Optional[str] = None
    notebook_id: Optional[str] = None
    parent_group_id: Optional[str] = None
    path: list[str] = field(default_factory=list)
    path_ids: list[str] = field(default_factory=list)
    sections: list[SectionNode] = field(default_factory=list)
    section_groups: list["SectionGroupNode"] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "section_group",
            "id": self.id,
            "display_name": self.display_name,
            "created_at": _iso(self.created_at),
            "modified_at": _iso(self.modified_at),
            "web_url": self.web_url,
            "notebook_id": self.notebook_id,
            "parent_group_id": self.parent_group_id,
            "path": list(self.path),
            "path_ids": list(self.path_ids),
            "sections": [s.to_dict() for s in self.sections],
            "section_groups": [g.to_dict() for g in self.section_groups],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SectionGroupNode":
        return cls(
            id=data["id"],
            display_name=data.get("display_name", ""),
            created_at=parse_graph_datetime(data.get("created_at")),
            modified_at=parse_graph_datetime(data.get("modified_at")),
            web_url=data.get("web_url"),
            notebook_id=data.get("notebook_id"),
            parent_group_id=data.get("parent_group_id"),
            path=list(data.get("path") or []),
            path_ids=list(data.get("path_ids") or []),
            sections=[SectionNode.from_dict(s) for s in data.get("sections") or []],
            section_groups=[SectionGroupNode.from_dict(g) for g in data.get("section_groups") or []],
            error=data.get("error"),
        )

    def iter_sections(self) -> Iterator[SectionNode]:
        yield from self.sections
        for group in self.section_groups:
            yield from group.iter_sections()


@dataclass(slots=True)
class NotebookNode:
    id: str
    display_name: str = ""
    created_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    web_url: Optional[str] = None
    client_url: Optional[str] = None
    is_default: bool = False
    is_shared: bool = False
    owner: Optional[str] = None
    last_modified_by: Optional[str] = None
    sections: list[SectionNode] = field(default_factory=list)
    section_groups: list[SectionGroupNode] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "notebook",
            "id": self.id,
            "display_name": self.display_name,
            "created_at": _iso(self.created_at),
            "modified_at": _iso(self.modified_at),
            "web_url": self.web_url,
            "client_url": self.client_url,
            "is_default": self.is_default,
            "is_shared": self.is_shared,
            "owner": self.owner,
            "last_modified_by": self.last_modified_by,
            "sections": [s.to_dict() for s in self.sections],
            "section_groups": [g.to_dict() for g in self.section_groups],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NotebookNode":
        return cls(
            id=data["id"],
            display_name=data.get("display_name", ""),
            created_at=parse_graph_datetime(data.get("created_at")),
            modified_at=parse_graph_datetime(data.get("modified_at")),
            web_url=data.get("web_url"),
            client_url=data.get("client_url"),
            is_default=bool(data.get("is_default", False)),
            is_shared=bool(data.get("is_shared", False)),
            owner=data.get("owner"),
            last_modified_by=data.get("last_modified_by"),
            sections=[SectionNode.from_dict(s) for s in data.get("sections") or []],
            section_groups=[SectionGroupNode.from_dict(g) for g in data.get("section_groups") or []],
            error=data.get("error"),
        )

    def iter_sections(self) -> Iterator[SectionNode]:
        yield from self.sections
        for group in self.section_groups:
            yield from group.iter_sections()

    def iter_pages(self) -> Iterator[PageRef]:
        for section in self.iter_sections():
            yield from section.pages


@dataclass(slots=True)
class NotebookTree:
    """The discovered hierarchy for one run."""

    notebooks: list[NotebookNode] = field(default_factory=list)
    discovered_at: datetime = field(default_factory=utcnow)
    schema_version: str = SCHEMA_VERSION

    def iter_sections(self) -> Iterator[SectionNode]:
        for notebook in self.notebooks:
            yield from notebook.iter_sections()

    def iter_pages(self) -> Iterator[PageRef]:
        for notebook in self.notebooks:
            yield from notebook.iter_pages()

    def counts(self) -> dict[str, int]:
        sections = list(self.iter_sections())
        groups = 0
        stack: list[SectionGroupNode] = []
        for notebook in self.notebooks:
            stack.extend(notebook.section_groups)
        while stack:
            group = stack.pop()
            groups += 1
            stack.extend(group.section_groups)
        return {
            "notebooks": len(self.notebooks),
            "section_groups": groups,
            "sections": len(sections),
            "pages": sum(len(s.pages) for s in sections),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "discovered_at": _iso(self.discovered_at),
            "counts": self.counts(),
            "notebooks": [n.to_dict() for n in self.notebooks],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NotebookTree":
        return cls(
            notebooks=[NotebookNode.from_dict(n) for n in data.get("notebooks") or []],
            discovered_at=parse_graph_datetime(data.get("discovered_at")) or utcnow(),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


# --------------------------------------------------------------------------- #
# Parsed page
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PageFlags:
    """Quick capability flags, useful for filtering and for known-gap reporting."""

    has_images: bool = False
    has_ink: bool = False
    has_tables: bool = False
    has_lists: bool = False
    has_attachments: bool = False
    has_math: bool = False
    has_links: bool = False
    has_onenote_links: bool = False
    has_unsupported: bool = False
    ocr_applied: bool = False
    text_truncated: bool = False

    def to_dict(self) -> dict[str, bool]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PageFlags":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: bool(v) for k, v in data.items() if k in names})


@dataclass(slots=True)
class PageMetadata:
    """Authorship, timestamps and source references for one page."""

    created_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    created_by: Optional[str] = None
    created_by_id: Optional[str] = None
    last_modified_by: Optional[str] = None
    last_modified_by_id: Optional[str] = None
    #: ``onenotePage.createdByAppId``.  Graph v1.0 does not expose a *user*
    #: identity on pages; notebook- and section-level authorship is carried on
    #: the hierarchy nodes instead.  See docs/onenote-parser.md.
    created_by_app_id: Optional[str] = None
    level: int = 0
    order: int = 0
    parent_page_id: Optional[str] = None
    web_url: Optional[str] = None
    client_url: Optional[str] = None
    content_url: Optional[str] = None
    self_url: Optional[str] = None
    fetched_at: Optional[datetime] = None
    api_version: str = "v1.0"

    def to_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        for key in ("created_at", "modified_at", "fetched_at"):
            out[key] = _iso(getattr(self, key))
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PageMetadata":
        names = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in names}
        for key in ("created_at", "modified_at", "fetched_at"):
            if key in kwargs:
                kwargs[key] = parse_graph_datetime(kwargs[key])
        return cls(**kwargs)


@dataclass(slots=True)
class ParsedPage:
    """A fully normalized OneNote page - the parser's unit of output."""

    id: str
    title: str = ""
    notebook_id: Optional[str] = None
    notebook_name: Optional[str] = None
    section_id: Optional[str] = None
    section_name: Optional[str] = None
    path: list[str] = field(default_factory=list)
    path_ids: list[str] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    text: str = ""
    metadata: PageMetadata = field(default_factory=PageMetadata)
    flags: PageFlags = field(default_factory=PageFlags)
    content_hash: str = ""
    warnings: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    @property
    def breadcrumb(self) -> str:
        """``Notebook > Group > Section > Page`` - the human-readable citation."""
        return " > ".join([*self.path, self.title] if self.title else list(self.path))

    def links(self) -> list[dict[str, Any]]:
        """Every hyperlink found on the page, in document order."""
        out: list[dict[str, Any]] = []
        for block in self.iter_blocks():
            for attr in ("content",):
                rich = getattr(block, attr, None)
                if isinstance(rich, RichText):
                    out.extend(rich.links)
            if isinstance(block, ListBlock):
                for item in block.items:
                    out.extend(item.content.links)
            elif isinstance(block, TableBlock):
                for row in block.rows:
                    for cell in row.cells:
                        out.extend(cell.content.links)
        return out

    def iter_blocks(self) -> Iterator[Block]:
        """Depth-first iteration including blocks nested in lists and tables."""
        for block in self.blocks:
            yield block
            if isinstance(block, ListBlock):
                for item in block.items:
                    yield from item.blocks
            elif isinstance(block, TableBlock):
                for row in block.rows:
                    for cell in row.cells:
                        yield from cell.blocks

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "title": self.title,
            "breadcrumb": self.breadcrumb,
            "notebook_id": self.notebook_id,
            "notebook_name": self.notebook_name,
            "section_id": self.section_id,
            "section_name": self.section_name,
            "path": list(self.path),
            "path_ids": list(self.path_ids),
            "metadata": self.metadata.to_dict(),
            "flags": self.flags.to_dict(),
            "content_hash": self.content_hash,
            "text": self.text,
            "blocks": [b.to_dict() for b in self.blocks],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ParsedPage":
        return cls(
            id=data["id"],
            title=data.get("title", ""),
            notebook_id=data.get("notebook_id"),
            notebook_name=data.get("notebook_name"),
            section_id=data.get("section_id"),
            section_name=data.get("section_name"),
            path=list(data.get("path") or []),
            path_ids=list(data.get("path_ids") or []),
            blocks=[block_from_dict(b) for b in data.get("blocks") or []],
            text=data.get("text", ""),
            metadata=PageMetadata.from_dict(data.get("metadata") or {}),
            flags=PageFlags.from_dict(data.get("flags") or {}),
            content_hash=data.get("content_hash", ""),
            warnings=list(data.get("warnings") or []),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


# --------------------------------------------------------------------------- #
# Change detection and run results
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PageChange:
    """How one page compared against the previous run."""

    page_id: str
    status: ChangeStatus
    title: str = ""
    path: list[str] = field(default_factory=list)
    previous_hash: Optional[str] = None
    current_hash: Optional[str] = None
    previous_modified_at: Optional[datetime] = None
    current_modified_at: Optional[datetime] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "status": self.status.value,
            "title": self.title,
            "path": list(self.path),
            "previous_hash": self.previous_hash,
            "current_hash": self.current_hash,
            "previous_modified_at": _iso(self.previous_modified_at),
            "current_modified_at": _iso(self.current_modified_at),
            "reason": self.reason,
        }


@dataclass(slots=True)
class ChangeSet:
    """All page-level changes observed during a run."""

    changes: list[PageChange] = field(default_factory=list)

    def add(self, change: PageChange) -> None:
        self.changes.append(change)

    def by_status(self, status: ChangeStatus) -> list[PageChange]:
        return [c for c in self.changes if c.status is status]

    def counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in ChangeStatus}
        for change in self.changes:
            counts[change.status.value] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {"counts": self.counts(), "changes": [c.to_dict() for c in self.changes]}


@dataclass(slots=True)
class RunStats:
    """Counters and timings for one parser run."""

    started_at: datetime = field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    notebooks: int = 0
    section_groups: int = 0
    sections: int = 0
    pages_discovered: int = 0
    pages_parsed: int = 0
    pages_skipped: int = 0
    pages_failed: int = 0
    images_downloaded: int = 0
    images_ocr: int = 0
    attachments_downloaded: int = 0
    attachments_extracted: int = 0
    bytes_downloaded: int = 0
    graph_requests: int = 0
    graph_retries: int = 0
    graph_throttled: int = 0

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        out["started_at"] = _iso(self.started_at)
        out["finished_at"] = _iso(self.finished_at)
        out["duration_seconds"] = self.duration_seconds
        return out


@dataclass(slots=True)
class ParseResult:
    """Everything a run produced.  Returned by :meth:`OneNoteParser.run`."""

    tree: NotebookTree = field(default_factory=NotebookTree)
    pages: list[ParsedPage] = field(default_factory=list)
    changes: ChangeSet = field(default_factory=ChangeSet)
    stats: RunStats = field(default_factory=RunStats)
    errors: list[dict[str, Any]] = field(default_factory=list)
    run_id: str = ""
    schema_version: str = SCHEMA_VERSION

    def page_by_id(self, page_id: str) -> Optional[ParsedPage]:
        for page in self.pages:
            if page.id == page_id:
                return page
        return None

    def to_dict(self, *, include_pages: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "stats": self.stats.to_dict(),
            "changes": self.changes.to_dict(),
            "errors": list(self.errors),
            "tree": self.tree.to_dict(),
        }
        if include_pages:
            out["pages"] = [p.to_dict() for p in self.pages]
        return out
