"""Text/Markdown rendering, content hashing and flag derivation."""

from __future__ import annotations

from onenote_parser.models import (
    HeadingBlock,
    ImageBlock,
    ListBlock,
    ListItem,
    Mark,
    MarkType,
    OcrResult,
    ParagraphBlock,
    RichText,
    TableBlock,
    TableCell,
    TableRow,
)
from onenote_parser.parsing import OneNoteHtmlParser
from onenote_parser.parsing.normalize import (
    compute_content_hash,
    render_markdown,
    render_text,
    summarize_flags,
)


def test_render_text_separates_blocks_with_blank_lines():
    blocks = [
        HeadingBlock(level=1, content=RichText("Title")),
        ParagraphBlock(content=RichText("Body")),
    ]
    text, truncated = render_text(blocks)
    assert text == "Title\n\nBody"
    assert truncated is False


def test_render_text_truncation_is_reported():
    text, truncated = render_text([ParagraphBlock(content=RichText("x" * 100))], max_chars=10)
    assert len(text) == 10 and truncated is True


def test_render_text_surfaces_ocr_and_alt_text_for_images():
    image = ImageBlock(alt_text="curve", ocr=OcrResult(text="4.5 mm/s", engine="tesseract"))
    text, _ = render_text([image])
    assert "[image: curve]" in text and "4.5 mm/s" in text


def test_ink_images_are_described_in_plain_text():
    text, _ = render_text([ImageBlock(is_ink=True)])
    assert text == "[handwritten/ink content]"


def test_markdown_reapplies_inline_marks_at_exact_offsets():
    rich = RichText(
        "see the spec now",
        [
            Mark(4, 12, MarkType.BOLD),
            Mark(8, 12, MarkType.LINK, {"href": "https://x"}),
        ],
    )
    assert render_markdown([ParagraphBlock(content=rich)]) == \
        "see **the [spec](https://x)** now"


def test_markdown_renders_checkboxes_and_nesting():
    listing = ListBlock(
        ordered=False,
        items=[
            ListItem(RichText("parent"), level=0, checked=False),
            ListItem(RichText("child"), level=1, checked=True),
        ],
    )
    assert render_markdown([listing]) == "- [ ] parent\n  - [x] child"


def test_markdown_of_full_fixture_is_stable(rich_html):
    blocks = OneNoteHtmlParser().parse(rich_html).blocks
    markdown = render_markdown(blocks)
    assert "## Scope and Purpose" in markdown
    assert "| Parameter | Limit | Notes |" in markdown
    assert "![P&ID excerpt](" in markdown
    assert markdown.count("```") == 2


# -- hashing ----------------------------------------------------------------- #


def test_hash_is_stable_across_reparses(rich_html):
    parser = OneNoteHtmlParser()
    first = compute_content_hash(parser.parse(rich_html).blocks, title="T", path=["A"])
    second = compute_content_hash(parser.parse(rich_html).blocks, title="T", path=["A"])
    assert first == second


def test_hash_changes_when_content_title_or_path_changes(rich_html):
    blocks = OneNoteHtmlParser().parse(rich_html).blocks
    base = compute_content_hash(blocks, title="T", path=["A"])
    assert compute_content_hash(blocks, title="T2", path=["A"]) != base
    assert compute_content_hash(blocks, title="T", path=["B"]) != base
    edited = OneNoteHtmlParser().parse(rich_html.replace("Never run the pump dry.", "Changed"))
    assert compute_content_hash(edited.blocks, title="T", path=["A"]) != base


def test_hash_ignores_local_download_bookkeeping():
    """Re-downloading resources to a new directory must not look like an edit."""
    def page(path):
        return [ImageBlock(resource_id="r1", alt_text="a", file_path=path,
                           sha256="deadbeef" if path else None,
                           byte_size=10 if path else None, downloaded=bool(path))]

    assert compute_content_hash(page(None)) == compute_content_hash(page("images/x.png"))


def test_hash_ignores_block_reordering_metadata_but_not_block_order():
    a = [ParagraphBlock(content=RichText("one"), order=0),
         ParagraphBlock(content=RichText("two"), order=1)]
    b = [ParagraphBlock(content=RichText("two"), order=0),
         ParagraphBlock(content=RichText("one"), order=1)]
    assert compute_content_hash(a) != compute_content_hash(b)


# -- flags ------------------------------------------------------------------- #


def test_flags_summarize_page_capabilities(rich_html):
    flags = summarize_flags(OneNoteHtmlParser().parse(rich_html).blocks)
    assert flags.has_images and flags.has_ink and flags.has_tables and flags.has_lists
    assert flags.has_attachments and flags.has_math and flags.has_unsupported
    assert flags.has_links and flags.has_onenote_links
    assert flags.ocr_applied is False


def test_ocr_applied_flag_requires_actual_text():
    empty = ImageBlock(ocr=OcrResult(text="", engine="tesseract"))
    filled = ImageBlock(ocr=OcrResult(text="hello", engine="tesseract"))
    assert summarize_flags([empty]).ocr_applied is False
    assert summarize_flags([filled]).ocr_applied is True


def test_flags_see_links_inside_table_cells():
    cell = TableCell(RichText("x", [Mark(0, 1, MarkType.LINK, {"href": "h", "kind": "onenote"})]))
    flags = summarize_flags([TableBlock(rows=[TableRow(cells=[cell])])])
    assert flags.has_links and flags.has_onenote_links
