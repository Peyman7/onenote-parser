"""Rendering, hashing and flag summarisation over normalized blocks.

These functions turn a block list into the derived artefacts every consumer
needs: a plain-text projection (chunking, search, diffing), a Markdown
projection (human review, migration) and a stable content hash (change
detection).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Iterator, Optional, Sequence

from ..models import (
    Block,
    CodeBlock,
    DividerBlock,
    FileBlock,
    HeadingBlock,
    ImageBlock,
    ListBlock,
    MarkType,
    MathBlock,
    PageFlags,
    ParagraphBlock,
    QuoteBlock,
    RichText,
    TableBlock,
    UnsupportedBlock,
)

__all__ = [
    "render_text",
    "render_markdown",
    "compute_content_hash",
    "summarize_flags",
    "iter_blocks",
    "VOLATILE_KEYS",
]

#: Fields excluded from the content hash because they change between runs
#: without the underlying OneNote content changing (local paths, download
#: bookkeeping, OCR timings).
VOLATILE_KEYS = frozenset(
    {
        "file_path", "downloaded", "byte_size", "sha256", "error",
        "extracted_text", "extraction_status", "extractor",
        "ocr", "source_url", "full_resource_url", "order",
    }
)


def iter_blocks(blocks: Iterable[Block]) -> Iterator[Block]:
    """Depth-first walk including blocks nested inside lists and tables."""
    for block in blocks:
        yield block
        if isinstance(block, ListBlock):
            for item in block.items:
                yield from iter_blocks(item.blocks)
        elif isinstance(block, TableBlock):
            for row in block.rows:
                for cell in row.cells:
                    yield from iter_blocks(cell.blocks)


def render_text(blocks: Sequence[Block], *, max_chars: int = 0) -> tuple[str, bool]:
    """Plain-text projection of a page.

    Returns ``(text, truncated)``.  Blocks are separated by blank lines so that
    downstream chunkers can split on paragraph boundaries.
    """
    parts: list[str] = []
    for block in blocks:
        rendered = _text_for_block(block)
        if rendered.strip():
            parts.append(rendered.rstrip())
    text = "\n\n".join(parts).strip()
    if max_chars and len(text) > max_chars:
        return text[:max_chars].rstrip(), True
    return text, False


def _text_for_block(block: Block) -> str:
    if isinstance(block, HeadingBlock):
        return block.content.text
    if isinstance(block, TableBlock):
        return block.to_markdown()
    if isinstance(block, ImageBlock):
        parts = []
        if block.alt_text:
            parts.append(f"[image: {block.alt_text}]")
        elif block.is_ink:
            parts.append("[handwritten/ink content]")
        else:
            parts.append("[image]")
        if block.ocr and block.ocr.text.strip():
            parts.append(block.ocr.text.strip())
        return "\n".join(parts)
    if isinstance(block, DividerBlock):
        return ""
    return block.text_content()


def render_markdown(blocks: Sequence[Block], *, image_link_base: Optional[str] = None) -> str:
    """Markdown projection of a page (GitHub-flavoured)."""
    lines: list[str] = []
    for block in blocks:
        lines.extend(_markdown_for_block(block, image_link_base))
        lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _markdown_for_block(block: Block, image_base: Optional[str]) -> list[str]:
    if isinstance(block, HeadingBlock):
        return [f"{'#' * block.level} {_inline_markdown(block.content)}"]
    if isinstance(block, ParagraphBlock):
        prefix = _todo_prefix(block.data_tag)
        return [prefix + _inline_markdown(block.content)]
    if isinstance(block, QuoteBlock):
        return [f"> {line}" for line in _inline_markdown(block.content).split("\n")]
    if isinstance(block, ListBlock):
        out: list[str] = []
        counters: dict[int, int] = {}
        for item in block.items:
            ordered = block.ordered if item.ordered is None else item.ordered
            indent = "  " * item.level
            if ordered:
                counters[item.level] = counters.get(item.level, 0) + 1
                bullet = f"{counters[item.level]}."
            else:
                bullet = "-"
            checkbox = ""
            if item.checked is not None:
                checkbox = "[x] " if item.checked else "[ ] "
            out.append(f"{indent}{bullet} {checkbox}{_inline_markdown(item.content)}")
            for nested in item.blocks:
                out.extend(f"{indent}  {line}" for line in _markdown_for_block(nested, image_base))
        return out
    if isinstance(block, TableBlock):
        return block.to_markdown().split("\n")
    if isinstance(block, CodeBlock):
        return [f"```{block.language or ''}", block.code, "```"]
    if isinstance(block, ImageBlock):
        alt = block.alt_text or ("handwritten/ink content" if block.is_ink else "image")
        target = block.file_path or block.full_resource_url or block.source_url or ""
        if image_base and block.file_path:
            target = f"{image_base.rstrip('/')}/{block.file_path.split('/')[-1]}"
        out = [f"![{alt}]({target})"]
        if block.ocr and block.ocr.text.strip():
            out.append("")
            out.append(f"> OCR: {block.ocr.text.strip()}")
        return out
    if isinstance(block, FileBlock):
        name = block.file_name or "attachment"
        target = block.file_path or block.source_url or ""
        out = [f"[:paperclip: {name}]({target})"]
        if block.extracted_text:
            out.append("")
            out.append("<details><summary>Extracted text</summary>")
            out.append("")
            out.append(block.extracted_text.strip())
            out.append("")
            out.append("</details>")
        return out
    if isinstance(block, MathBlock):
        if block.latex:
            return ["$$", block.latex, "$$"]
        return [block.text or (block.mathml or "")]
    if isinstance(block, DividerBlock):
        return ["---"]
    if isinstance(block, UnsupportedBlock):
        body = block.text or block.html
        return [f"<!-- unsupported:{block.tag} -->", body] if body else []
    return [block.text_content()]


def _todo_prefix(data_tag: Optional[str]) -> str:
    if not data_tag or not data_tag.lower().startswith("to-do"):
        return ""
    return "- [x] " if "completed" in data_tag.lower() else "- [ ] "


def _inline_markdown(rich: RichText) -> str:
    """Re-apply marks as Markdown, innermost-first so nesting stays valid."""
    if not rich.text:
        return ""
    if not rich.marks:
        return _escape_markdown(rich.text)

    wrappers = {
        MarkType.BOLD: ("**", "**"),
        MarkType.ITALIC: ("*", "*"),
        MarkType.CODE: ("`", "`"),
        MarkType.STRIKETHROUGH: ("~~", "~~"),
    }
    # Tokens at the same position must be ordered so spans nest correctly:
    # every closing token first (innermost span closes first), then every
    # opening token (widest span opens first).  Sorting by a flat "priority"
    # instead produced ``**the [spec**](href)`` for a link inside bold.
    inserts: list[tuple[int, int, int, int, str]] = []  # position, phase, key, seq, token
    for sequence, mark in enumerate(rich.marks):
        span = mark.end - mark.start
        if mark.type is MarkType.LINK:
            href = mark.attrs.get("href", "")
            open_token, close_token = "[", f"]({href})"
        elif mark.type in wrappers:
            open_token, close_token = wrappers[mark.type]
        else:
            continue
        inserts.append((mark.start, _OPEN, -span, sequence, open_token))
        inserts.append((mark.end, _CLOSE, span, -sequence, close_token))

    by_position: dict[int, list[tuple[int, int, int, str]]] = {}
    for position, phase, key, sequence, token in inserts:
        by_position.setdefault(position, []).append((phase, key, sequence, token))

    out: list[str] = []
    for index, char in enumerate(rich.text):
        for *_, token in sorted(by_position.get(index, [])):
            out.append(token)
        out.append(_escape_markdown(char))
    for *_, token in sorted(by_position.get(len(rich.text), [])):
        out.append(token)
    return "".join(out)


#: Token phases; closing tokens always precede opening tokens at a position.
_CLOSE, _OPEN = 0, 1

_MD_ESCAPE = {"*": "\\*", "_": "\\_", "`": "\\`", "[": "\\[", "]": "\\]"}


def _escape_markdown(text: str) -> str:
    if len(text) == 1:
        return _MD_ESCAPE.get(text, text)
    return "".join(_MD_ESCAPE.get(char, char) for char in text)


def compute_content_hash(
    blocks: Sequence[Block],
    *,
    title: str = "",
    path: Sequence[str] = (),
) -> str:
    """Stable SHA-256 over a page's normalized content.

    Volatile bookkeeping (local file paths, download state, OCR timings) is
    excluded so that re-running the parser on unchanged content yields the same
    hash, while a genuine edit, rename or move changes it.
    """
    canonical = {
        "title": title,
        "path": list(path),
        "blocks": [_canonical_block(b.to_dict()) for b in blocks],
    }
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_block(data: dict[str, Any]) -> Any:
    if isinstance(data, dict):
        return {
            key: _canonical_block(value)
            for key, value in sorted(data.items())
            if key not in VOLATILE_KEYS
        }
    if isinstance(data, list):
        return [_canonical_block(item) for item in data]
    return data


def summarize_flags(blocks: Sequence[Block], *, text_truncated: bool = False) -> PageFlags:
    """Derive the page-level capability flags from its blocks."""
    flags = PageFlags(text_truncated=text_truncated)
    for block in iter_blocks(blocks):
        if isinstance(block, ImageBlock):
            flags.has_images = True
            if block.is_ink:
                flags.has_ink = True
            if block.ocr and block.ocr.text.strip():
                flags.ocr_applied = True
        elif isinstance(block, TableBlock):
            flags.has_tables = True
        elif isinstance(block, ListBlock):
            flags.has_lists = True
        elif isinstance(block, FileBlock):
            flags.has_attachments = True
        elif isinstance(block, MathBlock):
            flags.has_math = True
        elif isinstance(block, UnsupportedBlock):
            flags.has_unsupported = True

        for rich in _rich_texts(block):
            for mark in rich.marks:
                if mark.type is MarkType.LINK:
                    flags.has_links = True
                    if mark.attrs.get("kind") == "onenote":
                        flags.has_onenote_links = True
    return flags


def _rich_texts(block: Block) -> Iterator[RichText]:
    content = getattr(block, "content", None)
    if isinstance(content, RichText):
        yield content
    if isinstance(block, ListBlock):
        for item in block.items:
            yield item.content
    elif isinstance(block, TableBlock):
        for row in block.rows:
            for cell in row.cells:
                yield cell.content
