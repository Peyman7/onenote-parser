#!/usr/bin/env python3
"""Parse OneNote HTML offline — no credentials, no network.

Useful for validating the parser against a page you captured earlier, or for
re-processing an archive of page HTML without re-crawling Graph.

    python examples/parse_local_html.py tests/fixtures/page_rich.html
"""

from __future__ import annotations

import sys
from pathlib import Path

from onenote_parser import ContentConfig, OneNoteHtmlParser
from onenote_parser.parsing import compute_content_hash, render_markdown, render_text, summarize_flags


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    html = Path(argv[0]).read_text(encoding="utf-8")
    parser = OneNoteHtmlParser(ContentConfig(include_ids=True))
    result = parser.parse(html)

    print(f"Title    : {result.title}")
    print(f"Blocks   : {len(result.blocks)}")
    print(f"Flags    : {[k for k, v in summarize_flags(result.blocks).to_dict().items() if v]}")
    print(f"Hash     : {compute_content_hash(result.blocks, title=result.title or '')[:16]}")
    if result.warnings:
        print(f"Warnings : {sorted(set(result.warnings))}")

    print("\n--- blocks -------------------------------------------------------")
    for block in result.blocks:
        preview = block.text_content().replace("\n", " ")[:60]
        print(f"{block.order:3d}  {block.type.value:12s} {block.source_id or '':18s} {preview}")

    print("\n--- links --------------------------------------------------------")
    for block in result.blocks:
        content = getattr(block, "content", None)
        for link in getattr(content, "links", []) or []:
            print(f"  [{link['kind']:8s}] {link['text']!r} -> {link['href'][:70]}")

    print("\n--- markdown -----------------------------------------------------")
    print(render_markdown(result.blocks)[:1200])

    text, truncated = render_text(result.blocks)
    print(f"\nPlain text: {len(text)} chars (truncated={truncated})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
