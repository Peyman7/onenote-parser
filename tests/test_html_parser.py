"""OneNote HTML normalization."""

from __future__ import annotations

import pytest

from onenote_parser.config import ContentConfig
from onenote_parser.errors import ParseError
from onenote_parser.models import (
    BlockType,
    CodeBlock,
    FileBlock,
    HeadingBlock,
    ImageBlock,
    ListBlock,
    MarkType,
    MathBlock,
    ParagraphBlock,
    TableBlock,
    UnsupportedBlock,
)
from onenote_parser.parsing import OneNoteHtmlParser


def parse(html: str, **config_kwargs):
    return OneNoteHtmlParser(ContentConfig(**config_kwargs)).parse(html)


def body(inner: str) -> str:
    return f"<html><head><title>T</title></head><body>{inner}</body></html>"


# -- structure --------------------------------------------------------------- #


def test_title_and_metadata_come_from_the_document_head(rich_html):
    result = parse(rich_html)
    assert result.title == "Pump Commissioning Checklist"
    assert result.created_at_raw == "2024-03-01T09:12:33.0000000"
    assert result.language == "en-US"


def test_generated_title_block_is_not_duplicated_as_content(rich_html):
    result = parse(rich_html)
    headings = [b.content.text for b in result.blocks if isinstance(b, HeadingBlock)]
    assert "Pump Commissioning Checklist" not in headings


def test_blocks_are_numbered_consecutively_in_document_order(rich_html):
    result = parse(rich_html)
    assert [b.order for b in result.blocks] == list(range(len(result.blocks)))


def test_absolutely_positioned_outlines_are_ordered_by_position(rich_html):
    """OneNote outlines are absolute-positioned; reading order follows top/left."""
    result = parse(rich_html)
    assert result.blocks[0].content.text.startswith("This outline is positioned above")

    unordered = parse(rich_html, order_by_position=False)
    assert not unordered.blocks[0].content.text.startswith("This outline is positioned above")


def test_source_ids_are_preserved_for_deep_linking(rich_html):
    result = parse(rich_html)
    ids = {b.source_id for b in result.blocks}
    assert {"h2-scope", "p-intro", "tbl-limits", "img-main", "obj-1"} <= ids


# -- inline formatting ------------------------------------------------------- #


def test_mark_offsets_survive_whitespace_collapsing():
    """Regression: collapsing whitespace twice used to shift every mark."""
    html = body(
        "<p>Alpha <b>bold</b> gamma\n      <span style='font-weight:bold'>delta</span>, "
        "see <a href='https://x'>link</a>.</p>"
    )
    rich = parse(html).blocks[0].content
    assert rich.text == "Alpha bold gamma delta, see link."
    spans = {m.type: rich.text[m.start:m.end] for m in rich.marks}
    assert spans[MarkType.BOLD] in ("bold", "delta")
    assert [rich.text[m.start:m.end] for m in rich.marks if m.type is MarkType.BOLD] == \
        ["bold", "delta"]
    assert [rich.text[m.start:m.end] for m in rich.marks if m.type is MarkType.LINK] == ["link"]


def test_nbsp_and_zero_width_characters_are_normalized():
    rich = parse(body("<p>a&nbsp;&nbsp;b​c</p>")).blocks[0].content
    assert rich.text == "a bc"


def test_br_becomes_a_newline_inside_a_paragraph():
    rich = parse(body("<p>line one<br/>line two</p>")).blocks[0].content
    assert rich.text == "line one\nline two"


@pytest.mark.parametrize(
    "html,mark",
    [
        ("<p><b>x</b></p>", MarkType.BOLD),
        ("<p><em>x</em></p>", MarkType.ITALIC),
        ("<p><u>x</u></p>", MarkType.UNDERLINE),
        ("<p><s>x</s></p>", MarkType.STRIKETHROUGH),
        ("<p><sup>x</sup></p>", MarkType.SUPERSCRIPT),
        ("<p><sub>x</sub></p>", MarkType.SUBSCRIPT),
        ("<p><span style='text-decoration:line-through'>x</span></p>", MarkType.STRIKETHROUGH),
        ("<p><span style='background-color:yellow'>x</span></p>", MarkType.HIGHLIGHT),
        ("<p><span style='font-weight:700'>x</span></p>", MarkType.BOLD),
        ("<p>run <code>init()</code> first</p>", MarkType.CODE),
    ],
)
def test_semantic_and_css_formatting_both_produce_marks(html, mark):
    rich = parse(body(html)).blocks[0].content
    assert mark in {m.type for m in rich.marks}


def test_nested_inline_marks_are_both_recorded():
    rich = parse(body("<p><b>bold <i>and italic</i></b></p>")).blocks[0].content
    kinds = {m.type: (m.start, m.end) for m in rich.marks}
    assert kinds[MarkType.BOLD] == (0, len(rich.text))
    assert rich.text[slice(*kinds[MarkType.ITALIC])] == "and italic"


def test_onenote_internal_links_are_classified_and_ids_extracted(rich_html):
    links = parse(rich_html).blocks[2].content.links
    internal = [link for link in links if link["kind"] == "onenote"]
    assert internal[0]["page_id"] == "{AAAA-BBBB}"
    assert internal[0]["section_id"] == "{11111111-2222-3333-4444-555555555555}"
    assert [link["kind"] for link in links if link["text"] == "ISO 9906"] == ["external"]


# -- lists ------------------------------------------------------------------- #


def test_nested_lists_are_flattened_with_levels(rich_html):
    listing = next(b for b in parse(rich_html).blocks if isinstance(b, ListBlock))
    assert [(i.level, i.content.text) for i in listing.items][:3] == [
        (0, "Verify alignment within tolerance"),
        (1, "Cold alignment < 0.05 mm"),
        (1, "Hot alignment < 0.10 mm"),
    ]


def test_todo_tags_become_checkbox_state(rich_html):
    listing = next(b for b in parse(rich_html).blocks if isinstance(b, ListBlock))
    checked = {i.content.text: i.checked for i in listing.items if i.checked is not None}
    assert checked == {"Confirm lube oil level": True, "Record baseline vibration": False}


def test_unordered_list_nested_in_ordered_list_keeps_both_kinds(rich_html):
    ordered = [b for b in parse(rich_html).blocks if isinstance(b, ListBlock)][1]
    assert ordered.ordered is True
    nested = [i for i in ordered.items if i.level == 1]
    assert nested and nested[0].ordered is False


# -- tables ------------------------------------------------------------------ #


def test_table_shape_header_detection_and_spans(rich_html):
    table = next(b for b in parse(rich_html).blocks if isinstance(b, TableBlock))
    assert table.has_header is True
    assert [c.content.text for c in table.rows[0].cells] == ["Parameter", "Limit", "Notes"]
    assert table.rows[2].cells[0].colspan == 2
    assert table.column_count == 3


def test_media_inside_a_table_cell_is_preserved_as_a_cell_block(rich_html):
    table = next(b for b in parse(rich_html).blocks if isinstance(b, TableBlock))
    nested = table.rows[2].cells[1].blocks
    assert isinstance(nested[0], ImageBlock)
    assert nested[0].alt_text == "temp curve"


def test_explicit_th_marks_a_header_row():
    table = parse(body("<table><tr><th><p>H</p></th></tr><tr><td><p>v</p></td></tr></table>")
                  ).blocks[0]
    assert table.has_header and table.rows[0].cells[0].header


def test_multi_paragraph_cell_joins_lines():
    table = parse(body("<table><tr><td><p>one</p><p>two</p></td></tr></table>")).blocks[0]
    assert table.rows[0].cells[0].content.text == "one\ntwo"


# -- media, math, unsupported ------------------------------------------------ #


def test_image_records_both_resolutions_and_resource_id(rich_html):
    image = next(b for b in parse(rich_html).blocks
                 if isinstance(b, ImageBlock) and b.source_id == "img-main")
    assert image.resource_id == "1-xyz!13"          # full-res wins for download
    assert image.source_url.endswith("1-xyz!12/$value")
    assert image.full_resource_url.endswith("1-xyz!13/$value")
    assert (image.width, image.height) == (640.0, 480.0)
    assert image.alt_text == "P&ID excerpt"


def test_ink_and_scanned_content_is_flagged(rich_html):
    ink = [b for b in parse(rich_html).blocks if isinstance(b, ImageBlock) and b.is_ink]
    assert len(ink) == 1 and ink[0].source_id == "img-ink"


def test_attachment_object_becomes_a_file_block(rich_html):
    attachment = next(b for b in parse(rich_html).blocks if isinstance(b, FileBlock))
    assert attachment.file_name == "commissioning-report.pdf"
    assert attachment.mime_type == "application/pdf"
    assert attachment.resource_id == "1-att!7"


def test_mathml_is_preserved_with_latex_annotation(rich_html):
    math = next(b for b in parse(rich_html).blocks if isinstance(b, MathBlock))
    assert math.latex.strip() == r"H = \frac{p}{\rho g}"
    assert math.mathml.startswith("<math")
    assert math.inline is False


def test_unknown_elements_are_preserved_verbatim(rich_html):
    unsupported = next(b for b in parse(rich_html).blocks if isinstance(b, UnsupportedBlock))
    assert unsupported.tag == "onenote-widget"
    assert "custom-attr" in unsupported.html
    assert unsupported.text == "Custom add-in payload"


def test_unsupported_html_can_be_dropped_while_keeping_the_text():
    result = parse(body("<widget a='1'>text</widget>"), preserve_unsupported_html=False)
    assert result.blocks[0].html == ""
    assert result.blocks[0].text == "text"
    assert "unsupported_element:widget" in result.warnings


# -- heuristics -------------------------------------------------------------- #


def test_monospace_paragraph_is_detected_as_code(rich_html):
    code = next(b for b in parse(rich_html).blocks if isinstance(b, CodeBlock))
    assert code.code == 'pump.status = "COMMISSIONED"'


def test_code_detection_can_be_disabled():
    html = body("<p style='font-family:Consolas'>x = 1</p>")
    assert isinstance(parse(html, detect_code_blocks=False).blocks[0], ParagraphBlock)


def test_bold_alone_is_emphasis_not_a_heading():
    """A bold line with no size cue stays a paragraph; see the heuristic docs."""
    result = parse(body("<p><b>Important</b></p>"))
    assert isinstance(result.blocks[0], ParagraphBlock)
    assert MarkType.BOLD in {m.type for m in result.blocks[0].content.marks}


def test_large_bold_paragraph_is_promoted_to_a_heading_without_bold_noise(rich_html):
    heading = next(b for b in parse(rich_html).blocks
                   if isinstance(b, HeadingBlock) and b.source_id == "p-bold-head")
    assert heading.content.text == "Pre-start checks"
    assert MarkType.BOLD not in {m.type for m in heading.content.marks}


def test_heading_inference_does_not_fire_on_ordinary_bold_sentences():
    html = body("<p><b>This is a long bold sentence that ends with a period.</b></p>")
    assert isinstance(parse(html).blocks[0], ParagraphBlock)


def test_heading_inference_can_be_disabled():
    html = body("<p style='font-size:18pt'><b>Short title</b></p>")
    assert isinstance(parse(html, infer_headings_from_style=False).blocks[0], ParagraphBlock)


# -- robustness -------------------------------------------------------------- #


def test_empty_html_yields_no_blocks_and_a_warning():
    result = parse("")
    assert result.blocks == [] and "empty_html" in result.warnings


def test_none_html_raises_parse_error():
    with pytest.raises(ParseError):
        OneNoteHtmlParser().parse(None)


def test_malformed_markup_still_parses():
    result = parse("<html><body><p>unclosed<div>next</body>")
    assert [b.text_content() for b in result.blocks] == ["unclosed", "next"]


def test_whitespace_only_paragraphs_never_become_blocks():
    html = body("<p></p><p>  </p><p>&nbsp;</p><p>real</p>")
    assert [b.text_content() for b in parse(html).blocks] == ["real"]


def test_block_level_children_inside_a_paragraph_are_hoisted():
    result = parse(body("<p>intro<table><tr><td><p>c</p></td></tr></table></p>"))
    assert [b.type for b in result.blocks] == [BlockType.PARAGRAPH, BlockType.TABLE]


def test_parser_is_reusable_across_pages(rich_html):
    parser = OneNoteHtmlParser()
    first = parser.parse(rich_html)
    second = parser.parse(rich_html)
    assert [b.to_dict() for b in first.blocks] == [b.to_dict() for b in second.blocks]
