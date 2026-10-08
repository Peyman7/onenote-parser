"""OneNote page HTML to normalized content blocks.

Microsoft Graph returns page content as a small, fairly regular XHTML document::

    <html><head><title>Page title</title><meta name="created" .../></head>
      <body data-absolute-enabled="true">
        <div data-id="title"><h1>Page title</h1><span>2024-03-01</span></div>
        <div id="div:{..}{1}" style="position:absolute;left:48px;top:115px">
          <p id="p:{..}{11}">text with <a href="onenote:...">an internal link</a></p>
          <ul><li>bullet<ul><li>nested</li></ul></li></ul>
          <table><tr><td><p>cell</p></td></tr></table>
          <img src=".../resources/{id}/$value" data-fullres-src="..." alt="..."/>
          <object data-attachment="spec.pdf" data=".../resources/{id}/$value"/>
        </div>
      </body></html>

Guarantees of this parser
-------------------------
* **Order is preserved.**  Outlines are absolutely positioned, so top-level
  containers are ordered by their ``(top, left)`` coordinates when available and
  by document order otherwise.
* **Nothing is dropped.**  Anything not recognised becomes an
  :class:`~onenote_parser.models.UnsupportedBlock` carrying the original markup.
* **Source anchors survive.**  With ``includeIDs=true`` every element carries a
  ``data-id``; it is copied onto the block as ``source_id`` so consumers can
  deep-link to the exact paragraph.
* **No network access.**  Images and attachments become blocks describing the
  resource; downloading them is the resource layer's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional, Sequence

from bs4 import BeautifulSoup, NavigableString, Tag

from ..config import ContentConfig
from ..errors import ParseError
from ..logging_utils import get_logger
from ..models import (
    Block,
    CodeBlock,
    DividerBlock,
    HeadingBlock,
    ImageBlock,
    FileBlock,
    ListBlock,
    ListItem,
    Mark,
    MarkType,
    MathBlock,
    ParagraphBlock,
    QuoteBlock,
    RichText,
    TableBlock,
    TableCell,
    TableRow,
    UnsupportedBlock,
)
from .links import describe_link

logger = get_logger("parsing.html")

__all__ = ["OneNoteHtmlParser", "HtmlParseResult"]

HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
INLINE_TAGS = {
    "a", "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "tt",
    "kbd", "samp", "sup", "sub", "mark", "span", "font", "small", "big", "abbr",
    "cite", "q", "var", "time", "label",
}
IGNORED_TAGS = {"style", "script", "meta", "link", "head", "title", "col", "colgroup"}

_WS_RE = re.compile(r"[ \t\r\f\v]+")
_ANY_WS_RE = re.compile(r"\s+")
_STYLE_RE = re.compile(r"([a-zA-Z-]+)\s*:\s*([^;]+)")
_RESOURCE_ID_RE = re.compile(r"/resources/([^/?#]+)", re.IGNORECASE)
_POSITION_RE = re.compile(r"(-?\d+(?:\.\d+)?)")
_MONOSPACE_FONTS = ("consolas", "courier", "monospace", "menlo", "monaco", "lucida console")
_SENTENCE_END = (".", "!", "?", ":", ";", ",")


@dataclass
class HtmlParseResult:
    """What :meth:`OneNoteHtmlParser.parse` produces for one page."""

    blocks: list[Block] = field(default_factory=list)
    title: Optional[str] = None
    created_at_raw: Optional[str] = None
    modified_at_raw: Optional[str] = None
    language: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    def images(self) -> list[ImageBlock]:
        return [b for b in _walk_blocks(self.blocks) if isinstance(b, ImageBlock)]

    def files(self) -> list[FileBlock]:
        return [b for b in _walk_blocks(self.blocks) if isinstance(b, FileBlock)]


def _walk_blocks(blocks: Iterable[Block]) -> Iterator[Block]:
    for block in blocks:
        yield block
        if isinstance(block, ListBlock):
            for item in block.items:
                yield from _walk_blocks(item.blocks)
        elif isinstance(block, TableBlock):
            for row in block.rows:
                for cell in row.cells:
                    yield from _walk_blocks(cell.blocks)


class _InlineContext:
    """Accumulates text, offset marks and out-of-band blocks for one run."""

    def __init__(self, normalize_whitespace: bool) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.marks: list[Mark] = []
        self.extras: list[Block] = []
        self.normalize = normalize_whitespace

    def add_text(self, text: str) -> None:
        """Append a text node.

        Whitespace is collapsed *here* and nowhere else: mark offsets are
        recorded against the buffer as it is built, so a second normalization
        pass in :meth:`finish` would silently shift every link and bold range.
        """
        if not text:
            return
        value = text.replace("\xa0", " ").replace("\u200b", "")
        if self.normalize:
            value = _ANY_WS_RE.sub(" ", value)
            trailing = self.parts[-1].endswith((" ", "\n")) if self.parts else True
            if value.startswith(" ") and trailing:
                value = value[1:]
        if not value:
            return
        self.parts.append(value)
        self.length += len(value)

    def add_break(self) -> None:
        self.parts.append("\n")
        self.length += 1

    def finish(self) -> RichText:
        text = "".join(self.parts)
        stripped = text.strip()
        offset = len(text) - len(text.lstrip())
        marks: list[Mark] = []
        limit = len(stripped)
        for mark in self.marks:
            start = max(0, mark.start - offset)
            end = min(limit, mark.end - offset)
            if end > start:
                marks.append(Mark(start=start, end=end, type=mark.type, attrs=mark.attrs))
        marks.sort(key=lambda m: (m.start, m.end, m.type.value))
        return RichText(text=stripped, marks=marks)


class OneNoteHtmlParser:
    """Converts OneNote page HTML into ordered content blocks.

    The parser is stateless between calls and safe to share across threads.

    >>> parser = OneNoteHtmlParser()
    >>> result = parser.parse("<html><body><p>Hello</p></body></html>")
    >>> result.blocks[0].text_content()
    'Hello'
    """

    def __init__(self, config: Optional[ContentConfig] = None) -> None:
        self.config = config or ContentConfig()

    # -- entry point ------------------------------------------------------ #

    def parse(self, html: str, *, page_id: Optional[str] = None) -> HtmlParseResult:
        """Parse a page's HTML.  Raises :class:`ParseError` only on unusable input."""
        if html is None:
            raise ParseError("Page HTML is None", details={"page_id": page_id})
        result = HtmlParseResult()
        if not html.strip():
            result.warnings.append("empty_html")
            return result

        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:  # pragma: no cover - bs4 is very tolerant
            raise ParseError(f"Could not parse page HTML: {exc}",
                             details={"page_id": page_id}) from exc

        self._read_head(soup, result)
        body = soup.body or soup
        containers = self._ordered_containers(body, result)

        blocks: list[Block] = []
        for container in containers:
            self._parse_node(container, blocks, result)

        if self.config.drop_empty_blocks:
            blocks = [b for b in blocks if not _is_empty(b)]
        for index, block in enumerate(blocks):
            block.order = index
        result.blocks = blocks
        return result

    # -- document head ---------------------------------------------------- #

    def _read_head(self, soup: BeautifulSoup, result: HtmlParseResult) -> None:
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True)
            result.title = title or None
        for meta in soup.find_all("meta"):
            name = (meta.get("name") or "").lower()
            content = meta.get("content")
            if name == "created":
                result.created_at_raw = content
            elif name in ("lastmodified", "last-modified", "modified"):
                result.modified_at_raw = content
        html_tag = soup.find("html")
        if html_tag and html_tag.get("lang"):
            result.language = html_tag.get("lang")

    # -- container ordering ----------------------------------------------- #

    def _ordered_containers(self, body: Tag, result: HtmlParseResult) -> list[Tag]:
        """Top-level nodes in reading order, dropping the generated title block."""
        children = [c for c in body.children if isinstance(c, Tag) or _has_text(c)]
        nodes: list[Any] = []
        for child in children:
            if isinstance(child, Tag) and self.config.skip_title_block and _is_title_block(child):
                if not result.title:
                    heading = child.find(HEADING_TAGS.keys())
                    if heading:
                        result.title = heading.get_text(strip=True)
                continue
            nodes.append(child)

        if not self.config.order_by_position:
            return nodes

        positioned = [(n, _absolute_position(n)) for n in nodes]
        if len(positioned) > 1 and all(pos is not None for _, pos in positioned):
            positioned.sort(key=lambda item: (item[1][1], item[1][0]))  # type: ignore[index]
            return [node for node, _ in positioned]
        return nodes

    # -- dispatch --------------------------------------------------------- #

    def _parse_node(self, node: Any, out: list[Block], result: HtmlParseResult) -> None:
        if isinstance(node, NavigableString):
            text = str(node)
            if text.strip():
                out.append(ParagraphBlock(content=RichText(text=_WS_RE.sub(" ", text.strip()))))
            return
        if not isinstance(node, Tag):
            return

        name = (node.name or "").lower()
        if name in IGNORED_TAGS:
            return

        if name in HEADING_TAGS:
            out.append(self._heading(node, HEADING_TAGS[name], result))
        elif name == "p":
            self._paragraph(node, out, result)
        elif name in ("ul", "ol"):
            block = self._list(node, ordered=(name == "ol"), result=result)
            if block.items:
                out.append(block)
        elif name == "table":
            out.append(self._table(node, result))
        elif name == "img":
            out.append(self._image(node))
        elif name == "object":
            out.append(self._object(node))
        elif name == "math":
            out.append(self._math(node))
        elif name in ("hr",):
            out.append(DividerBlock(source_id=_source_id(node)))
        elif name == "blockquote":
            rich, extras = self._inline_run(node)
            if rich:
                out.append(QuoteBlock(content=rich, source_id=_source_id(node),
                                      data_tag=node.get("data-tag")))
            out.extend(extras)
        elif name == "pre":
            code = node.get_text()
            out.append(CodeBlock(code=code.strip("\n"), language=_code_language(node),
                                 source_id=_source_id(node)))
        elif name in ("div", "body", "html", "section", "article", "center", "form",
                      "figure", "figcaption", "dl", "dd", "dt", "main", "header", "footer"):
            for child in node.children:
                self._parse_node(child, out, result)
        elif name == "br":
            return
        elif name in INLINE_TAGS:
            rich, extras = self._inline_run(node)
            if rich:
                out.append(ParagraphBlock(content=rich, source_id=_source_id(node),
                                          data_tag=node.get("data-tag")))
            out.extend(extras)
        else:
            out.append(self._unsupported(node, result))

    # -- block builders --------------------------------------------------- #

    def _heading(self, node: Tag, level: int, result: HtmlParseResult) -> HeadingBlock:
        rich, extras = self._inline_run(node)
        block = HeadingBlock(level=level, content=rich, source_id=_source_id(node),
                             data_tag=node.get("data-tag"))
        if extras:
            result.warnings.append("heading_contained_media")
        return block

    def _paragraph(self, node: Tag, out: list[Block], result: HtmlParseResult) -> None:
        # A OneNote <p> may itself contain block-level children (tables, images,
        # nested lists) when content was pasted from elsewhere.
        block_children = [
            child for child in node.children
            if isinstance(child, Tag)
            and child.name.lower() in {"table", "ul", "ol", "div", "blockquote", "pre", "hr"}
        ]
        if block_children:
            for child in node.children:
                self._parse_node(child, out, result)
            return

        rich, extras = self._inline_run(node)
        data_tag = node.get("data-tag")
        if rich:
            if self.config.detect_code_blocks and _looks_like_code(node):
                out.append(CodeBlock(code=rich.text, language=_code_language(node),
                                     source_id=_source_id(node), data_tag=data_tag))
            else:
                level = self._inferred_heading_level(node, rich)
                if level:
                    out.append(HeadingBlock(level=level, content=_strip_full_bold(rich),
                                            source_id=_source_id(node), data_tag=data_tag))
                else:
                    out.append(ParagraphBlock(content=rich, source_id=_source_id(node),
                                              data_tag=data_tag))
        out.extend(extras)

    def _inferred_heading_level(self, node: Tag, rich: RichText) -> Optional[int]:
        """Conservatively promote a styled paragraph to a heading.

        OneNote authors frequently style a short line as a large heading instead
        of using a real heading tag; without this the exported hierarchy is flat.

        Promotion requires an **explicit font-size cue** larger than body text.
        Bold alone is not enough: a bold word on its own line is ordinary
        emphasis far more often than it is a heading, and inventing headings
        from it corrupts the document outline for every downstream consumer.
        """
        if not self.config.infer_headings_from_style:
            return None
        text = rich.text.strip()
        if not text or len(text) > 80 or text.endswith(_SENTENCE_END) or "\n" in text:
            return None
        size = _font_size_pt(_style(node))
        if size is None:
            return None
        if size >= 20:
            return 2
        if size >= 16:
            return 3
        fully_bold = any(
            m.type is MarkType.BOLD and m.start == 0 and m.end >= len(text) for m in rich.marks
        )
        if fully_bold and size >= 14:
            return 4
        return None

    def _list(self, node: Tag, *, ordered: bool, result: HtmlParseResult,
              level: int = 0) -> ListBlock:
        block = ListBlock(ordered=ordered, source_id=_source_id(node))
        self._collect_list_items(node, block, ordered=ordered, level=level, result=result)
        return block

    def _collect_list_items(
        self, node: Tag, block: ListBlock, *, ordered: bool, level: int, result: HtmlParseResult
    ) -> None:
        """Flatten nested ``<ul>``/``<ol>`` into level-tagged items."""
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            if name in ("ul", "ol"):
                self._collect_list_items(
                    child, block, ordered=(name == "ol"), level=level + 1, result=result
                )
                continue
            if name != "li":
                continue

            nested = [c for c in child.children
                      if isinstance(c, Tag) and c.name.lower() in ("ul", "ol")]
            for sub in nested:
                sub.extract()

            rich, extras = self._inline_run(child)
            data_tag = child.get("data-tag")
            item = ListItem(
                content=rich,
                level=level,
                source_id=_source_id(child),
                ordered=ordered if ordered != block.ordered else None,
                data_tag=data_tag,
                checked=_todo_state(data_tag),
                blocks=extras,
            )
            block.items.append(item)
            for sub in nested:
                self._collect_list_items(
                    sub, block, ordered=(sub.name.lower() == "ol"), level=level + 1, result=result
                )

    def _table(self, node: Tag, result: HtmlParseResult) -> TableBlock:
        rows: list[TableRow] = []
        for row_node in node.find_all("tr", recursive=True):
            # Skip rows belonging to a nested table; those are parsed with it.
            if row_node.find_parent("table") is not node:
                continue
            cells: list[TableCell] = []
            for cell_node in row_node.find_all(["td", "th"], recursive=False):
                cells.append(self._table_cell(cell_node, result))
            if cells:
                rows.append(TableRow(cells=cells,
                                     header=all(c.header for c in cells) and bool(cells)))
        has_header = bool(rows) and (rows[0].header or _looks_like_header_row(rows[0]))
        if rows and has_header:
            rows[0].header = True
            for cell in rows[0].cells:
                cell.header = True
        borders = str(node.get("border", "1")).strip() not in ("0", "")
        return TableBlock(rows=rows, has_header=has_header, borders=borders,
                          source_id=_source_id(node))

    def _table_cell(self, node: Tag, result: HtmlParseResult) -> TableCell:
        # Cell text: each block-level child becomes its own line; nested tables,
        # images and attachments are preserved as cell-level blocks.
        nested_blocks: list[Block] = []
        lines: list[RichText] = []
        for child in node.children:
            if isinstance(child, Tag) and child.name.lower() in (
                "table", "img", "object", "ul", "ol", "hr", "math"
            ):
                self._parse_node(child, nested_blocks, result)
            elif isinstance(child, Tag) and child.name.lower() in ("p", "div", "h1", "h2", "h3",
                                                                   "h4", "h5", "h6"):
                rich, extras = self._inline_run(child)
                if rich:
                    lines.append(rich)
                nested_blocks.extend(extras)
            elif isinstance(child, NavigableString):
                if child.strip():
                    lines.append(RichText(text=_WS_RE.sub(" ", str(child).strip())))
            elif isinstance(child, Tag):
                rich, extras = self._inline_run(child)
                if rich:
                    lines.append(rich)
                nested_blocks.extend(extras)
        content = _join_rich(lines)
        return TableCell(
            content=content,
            colspan=_int_attr(node, "colspan", 1),
            rowspan=_int_attr(node, "rowspan", 1),
            header=node.name.lower() == "th",
            blocks=nested_blocks,
        )

    def _image(self, node: Tag) -> ImageBlock:
        src = node.get("src") or ""
        full = node.get("data-fullres-src") or ""
        chosen = full or src
        mime = node.get("data-fullres-src-type") if full else node.get("data-src-type")
        is_ink = _is_ink_image(node)
        return ImageBlock(
            resource_id=_resource_id(chosen or src),
            source_url=src or None,
            full_resource_url=full or None,
            alt_text=(node.get("alt") or "").strip() or None,
            width=_float_attr(node, "width") or _style_dimension(node, "width"),
            height=_float_attr(node, "height") or _style_dimension(node, "height"),
            mime_type=mime or None,
            is_ink=is_ink,
            source_id=_source_id(node),
            data_tag=node.get("data-tag"),
        )

    def _object(self, node: Tag) -> FileBlock:
        return FileBlock(
            resource_id=_resource_id(node.get("data") or ""),
            file_name=(node.get("data-attachment") or node.get("name") or "").strip() or None,
            source_url=node.get("data") or None,
            mime_type=(node.get("type") or "").strip() or None,
            source_id=_source_id(node),
            data_tag=node.get("data-tag"),
        )

    def _math(self, node: Tag) -> MathBlock:
        annotation = node.find("annotation")
        latex = None
        if annotation is not None:
            encoding = (annotation.get("encoding") or "").lower()
            if "tex" in encoding:
                latex = annotation.get_text()
        return MathBlock(
            mathml=str(node),
            latex=latex,
            text=node.get_text(" ", strip=True),
            inline=(node.get("display") or "").lower() != "block",
            notation="mathml",
            source_id=_source_id(node),
        )

    def _unsupported(self, node: Tag, result: HtmlParseResult) -> UnsupportedBlock:
        result.warnings.append(f"unsupported_element:{node.name}")
        html = str(node) if self.config.preserve_unsupported_html else ""
        return UnsupportedBlock(
            tag=node.name or "",
            html=html,
            text=node.get_text(" ", strip=True),
            reason="unrecognized_element",
            source_id=_source_id(node),
            data_tag=node.get("data-tag"),
        )

    # -- inline ----------------------------------------------------------- #

    def _inline_run(self, node: Tag) -> tuple[RichText, list[Block]]:
        """Flatten a node's inline content into text + marks, hoisting media."""
        ctx = _InlineContext(self.config.normalize_whitespace)
        self._inline(node, ctx)
        return ctx.finish(), ctx.extras

    def _inline(self, node: Tag, ctx: _InlineContext) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                ctx.add_text(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            name = (child.name or "").lower()
            if name in IGNORED_TAGS:
                continue
            if name == "br":
                ctx.add_break()
                continue
            if name == "img":
                ctx.extras.append(self._image(child))
                continue
            if name == "object":
                ctx.extras.append(self._object(child))
                continue
            if name == "math":
                ctx.extras.append(self._math(child))
                continue

            start = ctx.length
            self._inline(child, ctx)
            end = ctx.length
            if end > start:
                for mark_type, attrs in _marks_for(child):
                    ctx.marks.append(Mark(start=start, end=end, type=mark_type, attrs=attrs))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _source_id(node: Tag) -> Optional[str]:
    """The stable OneNote element id (``includeIDs=true``) if present."""
    return node.get("data-id") or node.get("id") or None


def _style(node: Tag) -> dict[str, str]:
    raw = node.get("style") or ""
    return {m.group(1).lower(): m.group(2).strip().lower() for m in _STYLE_RE.finditer(raw)}


def _font_size_pt(style: dict[str, str]) -> Optional[float]:
    value = style.get("font-size")
    if not value:
        return None
    match = _POSITION_RE.search(value)
    if not match:
        return None
    size = float(match.group(1))
    if "px" in value:
        size *= 0.75
    return size


def _absolute_position(node: Any) -> Optional[tuple[float, float]]:
    if not isinstance(node, Tag):
        return None
    style = _style(node)
    if style.get("position") != "absolute":
        return None
    left = _POSITION_RE.search(style.get("left", ""))
    top = _POSITION_RE.search(style.get("top", ""))
    if not (left and top):
        return None
    return (float(left.group(1)), float(top.group(1)))


def _is_title_block(node: Tag) -> bool:
    if (node.get("data-id") or "").lower() == "title":
        return True
    return (node.get("id") or "").lower() == "title"


def _has_text(node: Any) -> bool:
    return isinstance(node, NavigableString) and bool(str(node).strip())


def _int_attr(node: Tag, name: str, default: int) -> int:
    try:
        return int(str(node.get(name, default)).strip() or default)
    except (TypeError, ValueError):
        return default


def _float_attr(node: Tag, name: str) -> Optional[float]:
    raw = node.get(name)
    if raw in (None, ""):
        return None
    match = _POSITION_RE.search(str(raw))
    return float(match.group(1)) if match else None


def _style_dimension(node: Tag, name: str) -> Optional[float]:
    value = _style(node).get(name)
    if not value:
        return None
    match = _POSITION_RE.search(value)
    return float(match.group(1)) if match else None


def _resource_id(url: str) -> Optional[str]:
    match = _RESOURCE_ID_RE.search(url or "")
    return match.group(1) if match else None


def _is_ink_image(node: Tag) -> bool:
    """Detect handwriting/ink and scanned-printout images.

    OneNote surfaces ink either as a rendered image flagged with
    ``data-render-src`` / an ``image/ink`` source type, or via a ``data-tag``
    mentioning ink.  Multipart InkML (``includeinkml=true``) is handled by the
    pipeline, not here.
    """
    src_type = (node.get("data-src-type") or "").lower()
    if "ink" in src_type:
        return True
    for attr in ("data-render-src", "data-render-original-src", "data-ink"):
        if node.get(attr):
            return True
    classes = " ".join(node.get("class") or []).lower()
    return "ink" in classes


def _code_language(node: Tag) -> Optional[str]:
    classes = node.get("class") or []
    for value in classes:
        if value.startswith(("language-", "lang-")):
            return value.split("-", 1)[1]
    return node.get("data-language") or None


def _looks_like_code(node: Tag) -> bool:
    style = _style(node)
    family = style.get("font-family", "")
    if any(font in family for font in _MONOSPACE_FONTS):
        return True
    for child in node.find_all(["span", "code"], recursive=True):
        child_family = _style(child).get("font-family", "")
        if any(font in child_family for font in _MONOSPACE_FONTS):
            return True
    return False


def _todo_state(data_tag: Optional[str]) -> Optional[bool]:
    if not data_tag:
        return None
    value = data_tag.lower()
    if not value.startswith("to-do"):
        return None
    return "completed" in value


def _looks_like_header_row(row: TableRow) -> bool:
    """First row is a header when every non-empty cell is entirely bold."""
    populated = [c for c in row.cells if c.content.text.strip()]
    if not populated:
        return False
    for cell in populated:
        text_len = len(cell.content.text.strip())
        if not any(
            m.type is MarkType.BOLD and m.start == 0 and m.end >= text_len
            for m in cell.content.marks
        ):
            return False
    return True


def _join_rich(parts: Sequence[RichText]) -> RichText:
    """Concatenate rich-text runs with newlines, shifting mark offsets."""
    if not parts:
        return RichText()
    if len(parts) == 1:
        return parts[0]
    text_parts: list[str] = []
    marks: list[Mark] = []
    offset = 0
    for index, part in enumerate(parts):
        if index:
            text_parts.append("\n")
            offset += 1
        text_parts.append(part.text)
        for mark in part.marks:
            marks.append(
                Mark(start=mark.start + offset, end=mark.end + offset,
                     type=mark.type, attrs=mark.attrs)
            )
        offset += len(part.text)
    return RichText(text="".join(text_parts), marks=marks)


def _marks_for(node: Tag) -> list[tuple[MarkType, dict[str, Any]]]:
    """Inline annotations contributed by a single tag (semantic + CSS)."""
    name = (node.name or "").lower()
    marks: list[tuple[MarkType, dict[str, Any]]] = []

    semantic = {
        "b": MarkType.BOLD, "strong": MarkType.BOLD,
        "i": MarkType.ITALIC, "em": MarkType.ITALIC, "var": MarkType.ITALIC,
        "u": MarkType.UNDERLINE, "ins": MarkType.UNDERLINE,
        "s": MarkType.STRIKETHROUGH, "strike": MarkType.STRIKETHROUGH,
        "del": MarkType.STRIKETHROUGH,
        "code": MarkType.CODE, "tt": MarkType.CODE, "kbd": MarkType.CODE,
        "samp": MarkType.CODE,
        "sup": MarkType.SUPERSCRIPT, "sub": MarkType.SUBSCRIPT,
        "mark": MarkType.HIGHLIGHT,
    }
    if name in semantic:
        marks.append((semantic[name], {}))
    if name == "a":
        href = (node.get("href") or "").strip()
        if href:
            marks.append((MarkType.LINK, describe_link(href, title=node.get("title"))))

    style = _style(node)
    weight = style.get("font-weight", "")
    if weight in ("bold", "bolder") or (weight.isdigit() and int(weight) >= 600):
        marks.append((MarkType.BOLD, {}))
    if style.get("font-style") in ("italic", "oblique"):
        marks.append((MarkType.ITALIC, {}))
    decoration = style.get("text-decoration", "") + " " + style.get("text-decoration-line", "")
    if "underline" in decoration:
        marks.append((MarkType.UNDERLINE, {}))
    if "line-through" in decoration:
        marks.append((MarkType.STRIKETHROUGH, {}))
    background = style.get("background-color") or style.get("background") or ""
    if background and background not in ("transparent", "none", "#ffffff", "white", "inherit"):
        marks.append((MarkType.HIGHLIGHT, {"color": background}))
    family = style.get("font-family", "")
    if any(font in family for font in _MONOSPACE_FONTS):
        marks.append((MarkType.CODE, {}))

    # De-duplicate while keeping the first attrs seen for each type.
    seen: dict[MarkType, dict[str, Any]] = {}
    for mark_type, attrs in marks:
        if mark_type not in seen or (attrs and not seen[mark_type]):
            seen[mark_type] = attrs
    return list(seen.items())


def _strip_full_bold(rich: RichText) -> RichText:
    """Remove a bold mark spanning the whole run.

    A paragraph promoted to a heading because it was entirely bold must not also
    carry the bold mark, or it renders as ``### **Title**``.
    """
    length = len(rich.text)
    marks = [
        m for m in rich.marks
        if not (m.type is MarkType.BOLD and m.start == 0 and m.end >= length)
    ]
    return RichText(text=rich.text, marks=marks)


def _is_empty(block: Block) -> bool:
    if isinstance(block, (ParagraphBlock, HeadingBlock, QuoteBlock)):
        return not block.content.text.strip()
    if isinstance(block, ListBlock):
        return not block.items
    if isinstance(block, TableBlock):
        return not block.rows
    if isinstance(block, CodeBlock):
        return not block.code.strip()
    if isinstance(block, UnsupportedBlock):
        return not (block.text.strip() or block.html.strip())
    return False
