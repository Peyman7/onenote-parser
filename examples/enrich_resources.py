#!/usr/bin/env python3
"""Post-process exported resources and write the results back into the pages.

This is the pattern for any later enrichment pass -- OCR, vision captioning,
deeper attachment parsing -- run *after* an export instead of during the crawl:

    python examples/enrich_resources.py output-streetlight

Why it works
------------
``resources.json`` is a reverse index: one entry per distinct stored file, each
listing every page/block that references it.  Because the store deduplicates by
content hash, an image used on twenty pages is processed **once** and the result
is fanned out to all twenty references.

The join keys are ``page_id`` + ``source_id``.  ``source_id`` is the OneNote
element id captured with ``includeIDs=true``, so it is stable across runs: an
enrichment written today still lands on the right block after tomorrow's
incremental sync.

Older exports have no ``resources.json``; this rebuilds it from the page
documents when it is missing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from onenote_parser import ParsedPage
from onenote_parser.export.json_export import JsonExporter
from onenote_parser.models import ParseResult


def load_pages(output_dir: Path) -> list[tuple[Path, ParsedPage]]:
    """Read every exported page document back into the model."""
    pages = []
    for path in sorted((output_dir / "pages").glob("*.json")):
        pages.append((path, ParsedPage.from_dict(json.loads(path.read_text(encoding="utf-8")))))
    return pages


def load_or_build_index(output_dir: Path, pages: list[ParsedPage]) -> dict[str, Any]:
    """Use the exported resources.json, or rebuild it from the page documents."""
    path = output_dir / "resources.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    print("resources.json not found; rebuilding it from pages/ ...")
    index = JsonExporter._resource_index(ParseResult(pages=pages))
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  wrote {path}")
    return index


def describe(data: bytes, entry: dict[str, Any]) -> Optional[str]:
    """Stand-in for real OCR or a vision model.

    Replace with pytesseract, Azure Document Intelligence, Textract, or a
    multimodal LLM.  Return ``None`` to leave the block untouched.
    """
    return f"[{entry['type']} {entry.get('mime_type') or '?'}, {len(data)} bytes — plug OCR in here]"


def enrich(output_dir: Path, processor: Callable[[bytes, dict], Optional[str]]) -> int:
    paths_and_pages = load_pages(output_dir)
    pages = [page for _path, page in paths_and_pages]
    index = load_or_build_index(output_dir, pages)

    known_ids = {page.id for page in pages}
    # Block dataclasses use slots, so enrichment is collected here and applied
    # to the serialized documents rather than monkey-patched onto the models.
    enrichments: dict[tuple[str, str], str] = {}
    processed = skipped = 0

    for entry in index["resources"]:
        if not entry.get("file_path"):
            skipped += 1            # never downloaded; nothing to process
            continue
        target = output_dir / entry["file_path"]
        if not target.exists():
            skipped += 1
            continue

        # Processed once per distinct file, however many pages reference it.
        result = processor(target.read_bytes(), entry)
        if result is None:
            continue
        processed += 1

        for reference in entry["references"]:
            page_id, source_id = reference["page_id"], reference.get("source_id")
            if source_id and page_id in known_ids:
                enrichments[(page_id, source_id)] = result

    # Persist: re-serialize each touched page with the enrichment attached to
    # the matching block.
    updated = 0
    for path, page in paths_and_pages:
        if not any(key[0] == page.id for key in enrichments):
            continue
        payload = page.to_dict()
        touched = False
        for serialized in _walk(payload["blocks"]):
            key = (page.id, serialized.get("source_id"))
            if key in enrichments:
                serialized["enrichment"] = enrichments[key]
                touched = True
        if touched:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            updated += 1

    print(f"\nprocessed {processed} distinct resource(s), skipped {skipped}, "
          f"updated {updated} page document(s)")
    print(f"references covered: {index['counts']['total_references']} "
          f"across {index['counts']['distinct_resources']} file(s)")
    return 0


def _walk(blocks: list[dict[str, Any]]):
    """Depth-first walk of serialized blocks, matching ParsedPage.iter_blocks()."""
    for block in blocks:
        yield block
        if block.get("type") == "list":
            for item in block.get("items", []):
                yield from _walk(item.get("blocks", []))
        elif block.get("type") == "table":
            for row in block.get("rows", []):
                for cell in row.get("cells", []):
                    yield from _walk(cell.get("blocks", []))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    output_dir = Path(argv[0])
    if not (output_dir / "pages").is_dir():
        print(f"No pages/ directory under {output_dir}", file=sys.stderr)
        return 1
    return enrich(output_dir, describe)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
