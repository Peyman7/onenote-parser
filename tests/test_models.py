"""The output contract: serialization round-trips and stable shapes."""

from __future__ import annotations

from datetime import datetime, timezone

from onenote_parser.models import (
    SCHEMA_VERSION,
    BlockType,
    ChangeStatus,
    HeadingBlock,
    ImageBlock,
    ListBlock,
    ListItem,
    Mark,
    MarkType,
    NotebookNode,
    NotebookTree,
    OcrResult,
    PageChange,
    PageRef,
    ParagraphBlock,
    ParsedPage,
    RichText,
    SectionGroupNode,
    SectionNode,
    TableBlock,
    TableCell,
    TableRow,
    block_from_dict,
    parse_graph_datetime,
)


def test_every_block_type_round_trips():
    from onenote_parser.models import (
        CodeBlock, DividerBlock, FileBlock, MathBlock, QuoteBlock, UnsupportedBlock,
    )

    blocks = [
        HeadingBlock(level=2, content=RichText("Title"), source_id="h1"),
        ParagraphBlock(content=RichText("Body", [Mark(0, 4, MarkType.BOLD)])),
        ListBlock(ordered=True, items=[ListItem(RichText("a"), level=0, checked=False)]),
        TableBlock(rows=[TableRow(cells=[TableCell(RichText("c"), colspan=2)])], has_header=True),
        ImageBlock(resource_id="r1", alt_text="alt", ocr=OcrResult(text="ocr", engine="e")),
        FileBlock(resource_id="f1", file_name="a.pdf", extracted_text="text"),
        CodeBlock(code="x = 1", language="python"),
        QuoteBlock(content=RichText("quoted")),
        MathBlock(latex="a^2", text="a^2", notation="mathml"),
        DividerBlock(),
        UnsupportedBlock(tag="widget", html="<widget/>", text="w"),
    ]
    for index, block in enumerate(blocks):
        block.order = index
        payload = block.to_dict()
        assert payload["type"] in {b.value for b in BlockType}
        assert block_from_dict(payload).to_dict() == payload


def test_unknown_block_type_degrades_to_unsupported():
    block = block_from_dict({"type": "hologram", "order": 3, "text": "?"})
    assert block.type is BlockType.UNSUPPORTED
    assert block.reason == "unknown_block_type"
    assert block.order == 3


def test_richtext_links_expose_span_text():
    rich = RichText("see the spec", [Mark(8, 12, MarkType.LINK, {"href": "https://x", "kind": "external"})])
    assert rich.links == [{"href": "https://x", "kind": "external", "text": "spec"}]


def test_table_markdown_escapes_pipes_and_newlines():
    table = TableBlock(
        rows=[
            TableRow(cells=[TableCell(RichText("a|b")), TableCell(RichText("l1\nl2"))], header=True),
            TableRow(cells=[TableCell(RichText("1")), TableCell(RichText("2"))]),
        ],
        has_header=True,
    )
    markdown = table.to_markdown()
    assert "a\\|b" in markdown and "l1<br>l2" in markdown
    assert markdown.splitlines()[1] == "| --- | --- |"


def test_parsed_page_round_trip_and_breadcrumb():
    page = ParsedPage(
        id="pg-1",
        title="Welcome",
        path=["Engineering", "Pumps", "General"],
        blocks=[ParagraphBlock(content=RichText("hi"))],
        text="hi",
        content_hash="abc",
    )
    payload = page.to_dict()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["breadcrumb"] == "Engineering > Pumps > General > Welcome"
    assert ParsedPage.from_dict(payload).to_dict() == payload


def test_tree_counts_include_nested_section_groups():
    inner = SectionGroupNode(id="g2", sections=[SectionNode(id="s2", pages=[PageRef("p2")])])
    outer = SectionGroupNode(id="g1", section_groups=[inner],
                             sections=[SectionNode(id="s1", pages=[PageRef("p1")])])
    tree = NotebookTree(notebooks=[NotebookNode(id="nb", section_groups=[outer])])
    assert tree.counts() == {"notebooks": 1, "section_groups": 2, "sections": 2, "pages": 2}
    assert [p.id for p in tree.iter_pages()] == ["p1", "p2"]
    assert NotebookTree.from_dict(tree.to_dict()).counts() == tree.counts()


def test_change_set_counts_every_status():
    from onenote_parser.models import ChangeSet

    changes = ChangeSet()
    for status in ChangeStatus:
        changes.add(PageChange(page_id=status.value, status=status))
    assert changes.counts() == {s.value: 1 for s in ChangeStatus}
    assert changes.by_status(ChangeStatus.DELETED)[0].page_id == "deleted"


def test_parse_graph_datetime_handles_graph_formats_and_junk():
    assert parse_graph_datetime("2024-03-01T09:12:33.7Z").tzinfo == timezone.utc
    assert parse_graph_datetime("2024-03-01T09:12:33+02:00").hour == 9
    assert parse_graph_datetime("") is None
    assert parse_graph_datetime("not-a-date") is None
    naive = parse_graph_datetime(datetime(2024, 1, 1))
    assert naive.tzinfo == timezone.utc


def test_iter_blocks_reaches_nested_table_and_list_children():
    image = ImageBlock(resource_id="nested")
    table = TableBlock(rows=[TableRow(cells=[TableCell(RichText("c"), blocks=[image])])])
    listing = ListBlock(items=[ListItem(RichText("i"), blocks=[ImageBlock(resource_id="in-list")])])
    page = ParsedPage(id="p", blocks=[table, listing])
    ids = [b.resource_id for b in page.iter_blocks() if isinstance(b, ImageBlock)]
    assert ids == ["nested", "in-list"]
