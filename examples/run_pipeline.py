#!/usr/bin/env python3
"""Run the full pipeline from Python, with a custom OCR engine.

    export ONENOTE_CLIENT_ID=... ONENOTE_TENANT_ID=... ONENOTE_REFRESH_TOKEN=...
    python examples/run_pipeline.py

Shows the three extension points a host application most often needs:
a custom token source, a custom OCR engine, and a progress callback.
"""

from __future__ import annotations

import os
import sys

from onenote_parser import ChangeStatus, OneNoteParser, ParserConfig, configure_logging
from onenote_parser.resources import CallableOcrEngine


def caption_image(data: bytes) -> str:
    """Stand-in for a real OCR service or vision model.

    Replace the body with a call to Tesseract, Azure Document Intelligence,
    Textract, or a multimodal LLM.  Anything that returns text works; failures
    are captured on the block rather than raised.
    """
    return f"[{len(data)} bytes of image data — plug a real OCR engine in here]"


def on_progress(kind: str, payload: dict) -> None:
    if kind in ("notebook", "section"):
        print(f"  discovering {kind}: {payload.get('name')}")


def main() -> int:
    configure_logging("INFO")

    config = ParserConfig.load(os.environ.get("ONENOTE_CONFIG") or None)
    config.export.formats = ("json", "jsonl", "markdown")
    config.filters.max_pages = 25          # keep the example cheap
    config.ocr.enabled = True
    config.ocr.min_width = 1
    config.ocr.min_height = 1

    try:
        config.validate()
    except Exception as exc:                # ConfigurationError
        print(f"Configuration problem: {exc}", file=sys.stderr)
        print("Set ONENOTE_CLIENT_ID / ONENOTE_TENANT_ID / ONENOTE_REFRESH_TOKEN, "
              "or point ONENOTE_CONFIG at a config file.", file=sys.stderr)
        return 2

    with OneNoteParser(
        config,
        ocr_engine=CallableOcrEngine(caption_image, name="example"),
        progress=on_progress,
    ) as parser:
        result = parser.run()

    print(f"\nRun {result.run_id}: {result.stats.pages_parsed} page(s) in "
          f"{result.stats.duration_seconds:.1f}s")
    for status in (ChangeStatus.NEW, ChangeStatus.MODIFIED, ChangeStatus.DELETED,
                   ChangeStatus.INACCESSIBLE):
        for change in result.changes.by_status(status):
            print(f"  {status.value:13s} {' > '.join(change.path)} > {change.title}")

    # The normalized pages are what a downstream system consumes.
    for page in result.pages[:3]:
        print(f"\n{page.breadcrumb}")
        print(f"  {len(page.blocks)} blocks, {len(page.text)} chars, hash {page.content_hash[:12]}")
        for link in page.links():
            if link["kind"] == "onenote":
                print(f"  internal link -> page_id={link.get('page_id')}")

    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
