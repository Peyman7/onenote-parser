"""Downloading and post-processing of page resources (images, attachments)."""

from .attachments import (
    AttachmentProcessor,
    TextExtractor,
    extractor_for,
    register_extractor,
)
from .images import ImageProcessor, ResourceStore
from .ocr import (
    CallableOcrEngine,
    NullOcrEngine,
    OcrCache,
    OcrEngine,
    TesseractOcrEngine,
    create_ocr_engine,
)

__all__ = [
    "ImageProcessor",
    "ResourceStore",
    "AttachmentProcessor",
    "TextExtractor",
    "register_extractor",
    "extractor_for",
    "OcrEngine",
    "NullOcrEngine",
    "TesseractOcrEngine",
    "CallableOcrEngine",
    "OcrCache",
    "create_ocr_engine",
]
