"""OneNote HTML -> normalized content blocks."""

from .html_parser import HtmlParseResult, OneNoteHtmlParser
from .links import classify_link, extract_onenote_ids
from .normalize import (
    compute_content_hash,
    render_markdown,
    render_text,
    summarize_flags,
)

__all__ = [
    "OneNoteHtmlParser",
    "HtmlParseResult",
    "classify_link",
    "extract_onenote_ids",
    "render_text",
    "render_markdown",
    "compute_content_hash",
    "summarize_flags",
]
