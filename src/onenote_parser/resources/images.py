"""Image download, storage and optional OCR."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Optional

from ..config import ImageConfig, OcrConfig
from ..logging_utils import get_logger
from ..models import ImageBlock, OcrResult
from .ocr import NullOcrEngine, OcrCache, OcrEngine
from .store import ResourceStore, extension_for_mime, sha256_hex

logger = get_logger("resources.images")

__all__ = ["ImageProcessor", "ResourceStore"]

_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)


def sniff_mime(data: bytes) -> Optional[str]:
    """Identify an image from its magic bytes (OneNote's headers can lie)."""
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:256].lstrip()
    if head.startswith(b"<?xml") and b"<svg" in data[:1024]:
        return "image/svg+xml"
    if head.startswith(b"<svg"):
        return "image/svg+xml"
    return None


def _resolve_resource_url(client: Any, url: Optional[str]) -> Optional[str]:
    """Let the Graph client rewrite a resource URL into a form it can fetch.

    OneNote emits unusable ``siteCollections/...`` resource URLs for
    site-hosted notebooks; :meth:`GraphClient.resource_url` repairs them.  A
    custom client that does not implement it keeps the original URL.
    """
    if not url:
        return url
    rewrite = getattr(client, "resource_url", None)
    return rewrite(url) if callable(rewrite) else url


class ImageProcessor:
    """Downloads an :class:`ImageBlock`'s bytes and enriches the block.

    Responsibilities are deliberately narrow: fetch, verify, store, hash,
    optionally OCR.  Layout and ordering stay with the HTML parser.
    """

    def __init__(
        self,
        client: Any,
        config: ImageConfig,
        store: ResourceStore,
        *,
        ocr_engine: Optional[OcrEngine] = None,
        ocr_config: Optional[OcrConfig] = None,
        ocr_cache: Optional[OcrCache] = None,
        output_root: Optional[Path] = None,
    ) -> None:
        self.client = client
        self.config = config
        self.store = store
        self.ocr_engine = ocr_engine or NullOcrEngine()
        self.ocr_config = ocr_config or OcrConfig()
        self.ocr_cache = ocr_cache
        self.output_root = output_root
        self.downloaded = 0
        self.ocr_count = 0
        self.bytes_downloaded = 0

    # -- main ------------------------------------------------------------- #

    def process(self, block: ImageBlock) -> ImageBlock:
        """Populate ``block`` in place.  Never raises for a single bad image."""
        if not self.config.download:
            return block
        cached = self._reuse(block)
        if cached is not None:
            return cached

        url = self._pick_url(block)
        if not url:
            block.error = "no_source_url"
            return block

        try:
            data, headers = self.client.get_bytes(url, accept="image/*")
        except Exception as exc:  # noqa: BLE001 - a broken image must not fail the page
            block.error = f"download_failed: {exc}"
            logger.warning("Image download failed (%s): %s", block.resource_id or url, exc)
            return block

        if not data:
            block.error = "empty_response"
            return block
        if len(data) > self.config.max_bytes:
            block.error = f"too_large:{len(data)}"
            block.byte_size = len(data)
            logger.info("Image %s exceeds max_bytes (%d)", block.resource_id, len(data))
            return block
        if len(data) < self.config.min_bytes:
            # Spacer/bullet artwork: record the size but do not clutter the store.
            block.byte_size = len(data)
            block.error = f"below_min_bytes:{len(data)}"
            return block

        mime = (
            sniff_mime(data)
            or block.mime_type
            or (headers.get("content-type") or "").split(";")[0]
            or None
        )
        block.mime_type = mime
        digest = sha256_hex(data)
        name = f"{block.resource_id or digest[:12]}{extension_for_mime(mime, default='.img')}"
        path, checksum, reused = self.store.store(
            data, filename=name, mime_type=mime, digest=digest
        )
        block.file_path = self.store.relative(path, base=self.output_root)
        block.sha256 = checksum
        block.byte_size = len(data)
        block.downloaded = True
        if not reused:
            self.downloaded += 1
            self.bytes_downloaded += len(data)

        self.store.remember(block.resource_id, {
            "file_path": block.file_path, "sha256": checksum,
            "byte_size": len(data), "mime_type": mime,
        })
        self._maybe_ocr(block, data)
        return block

    def _reuse(self, block: ImageBlock) -> Optional[ImageBlock]:
        """Populate the block from a previously downloaded file, if available."""
        if not self.config.reuse_downloaded:
            return None
        entry = self.store.lookup(block.resource_id)
        if entry is None:
            return None
        path = self.store.root.parent / entry["file_path"]
        block.file_path = entry["file_path"]
        block.sha256 = entry.get("sha256")
        block.byte_size = entry.get("byte_size")
        block.mime_type = entry.get("mime_type") or block.mime_type
        block.downloaded = True
        logger.debug("Reusing cached image %s", block.resource_id)
        if self.ocr_config.enabled and not isinstance(self.ocr_engine, NullOcrEngine):
            try:
                self._maybe_ocr(block, path.read_bytes())
            except OSError as exc:  # pragma: no cover - file vanished
                logger.warning("Could not read cached image %s: %s", path, exc)
        return block

    def data_uri(self, data: bytes, mime: Optional[str]) -> str:
        """Build a ``data:`` URI (used by exporters when ``embed_base64``)."""
        return f"data:{mime or 'application/octet-stream'};base64,{base64.b64encode(data).decode()}"

    # -- internals -------------------------------------------------------- #

    def _pick_url(self, block: ImageBlock) -> Optional[str]:
        if self.config.prefer_full_resolution and block.full_resource_url:
            chosen = block.full_resource_url
        else:
            chosen = block.source_url or block.full_resource_url
        return _resolve_resource_url(self.client, chosen)

    def _maybe_ocr(self, block: ImageBlock, data: bytes) -> None:
        config = self.ocr_config
        if not config.enabled or isinstance(self.ocr_engine, NullOcrEngine):
            return
        if block.is_ink and not config.include_ink:
            return
        if len(data) > config.max_bytes:
            block.ocr = OcrResult(text="", engine=self.ocr_engine.name, error="image_too_large")
            return
        if block.width and block.height:
            if block.width < config.min_width or block.height < config.min_height:
                return

        language = "+".join(config.languages)
        cache_key = OcrCache.key(block.sha256 or sha256_hex(data), self.ocr_engine.name, language)
        if self.ocr_cache:
            cached = self.ocr_cache.get(cache_key)
            if cached is not None:
                block.ocr = cached
                return

        result = self.ocr_engine.recognize(data, mime_type=block.mime_type)
        block.ocr = result
        if result.text.strip():
            self.ocr_count += 1
        if self.ocr_cache:
            self.ocr_cache.put(cache_key, result)
