"""Optional OCR over downloaded images.

OCR is strictly opt-in (``ocr.enabled``) and pluggable: the pipeline only needs
an object with ``recognize(data, mime_type) -> OcrResult``.  Hosts that already
run a vision model or a cloud OCR service implement that one method (or wrap a
function in :class:`CallableOcrEngine`) and skip Tesseract entirely.
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

from ..config import OcrConfig
from ..logging_utils import get_logger
from ..models import OcrResult

logger = get_logger("resources.ocr")

__all__ = [
    "OcrEngine",
    "NullOcrEngine",
    "TesseractOcrEngine",
    "CallableOcrEngine",
    "OcrCache",
    "create_ocr_engine",
]


@runtime_checkable
class OcrEngine(Protocol):
    """Anything that can turn image bytes into text."""

    name: str

    def recognize(self, data: bytes, *, mime_type: Optional[str] = None) -> OcrResult:
        ...


class NullOcrEngine:
    """No-op engine used when OCR is disabled.  Always returns empty text."""

    name = "null"

    def recognize(self, data: bytes, *, mime_type: Optional[str] = None) -> OcrResult:
        return OcrResult(text="", engine=self.name)


class CallableOcrEngine:
    """Adapts ``fn(bytes) -> str`` (or ``-> OcrResult``) into an engine."""

    def __init__(self, fn: Callable[[bytes], Any], *, name: str = "callable") -> None:
        self._fn = fn
        self.name = name

    def recognize(self, data: bytes, *, mime_type: Optional[str] = None) -> OcrResult:
        started = time.monotonic()
        try:
            value = self._fn(data)
        except Exception as exc:  # noqa: BLE001 - engine failure must not kill a run
            logger.warning("OCR engine %s failed: %s", self.name, exc)
            return OcrResult(text="", engine=self.name, error=str(exc))
        if isinstance(value, OcrResult):
            return value
        return OcrResult(
            text=str(value or ""),
            engine=self.name,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


class TesseractOcrEngine:
    """Tesseract OCR via ``pytesseract``.

    Install with ``pip install 'onenote-parser[ocr]'`` **and** the native
    ``tesseract`` binary (``apt-get install tesseract-ocr``).
    """

    name = "tesseract"

    def __init__(
        self,
        *,
        languages: Sequence[str] = ("eng",),
        timeout_seconds: float = 60.0,
        min_confidence: Optional[float] = None,
        tesseract_cmd: Optional[str] = None,
    ) -> None:
        self.languages = list(languages) or ["eng"]
        self.timeout_seconds = timeout_seconds
        self.min_confidence = min_confidence
        self._tesseract_cmd = tesseract_cmd
        self._checked = False

    def _ensure_ready(self):  # type: ignore[no-untyped-def]
        import pytesseract  # noqa: F401  (import errors surface to the caller)
        from PIL import Image  # noqa: F401

        import pytesseract as pt

        if self._tesseract_cmd and not self._checked:
            pt.pytesseract.tesseract_cmd = self._tesseract_cmd
        self._checked = True
        return pt

    def recognize(self, data: bytes, *, mime_type: Optional[str] = None) -> OcrResult:
        started = time.monotonic()
        language = "+".join(self.languages)
        try:
            pt = self._ensure_ready()
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                image.load()
                frame = image.convert("L") if image.mode not in ("L", "RGB") else image
                payload = pt.image_to_data(
                    frame, lang=language, timeout=self.timeout_seconds,
                    output_type=pt.Output.DICT,
                )
        except ImportError as exc:
            return OcrResult(
                text="", engine=self.name, language=language,
                error=f"OCR dependencies missing ({exc}); pip install 'onenote-parser[ocr]'",
            )
        except Exception as exc:  # noqa: BLE001 - unreadable image, missing binary, timeout
            logger.warning("Tesseract OCR failed: %s", exc)
            return OcrResult(text="", engine=self.name, language=language, error=str(exc))

        words: list[str] = []
        confidences: list[float] = []
        for text, confidence in zip(payload.get("text", []), payload.get("conf", [])):
            if not str(text).strip():
                continue
            words.append(str(text))
            try:
                value = float(confidence)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                confidences.append(value)

        mean_confidence = sum(confidences) / len(confidences) if confidences else None
        text_value = " ".join(words).strip()
        if self.min_confidence is not None and mean_confidence is not None:
            if mean_confidence < self.min_confidence:
                logger.debug("Discarding OCR text below confidence threshold (%.1f)", mean_confidence)
                text_value = ""

        return OcrResult(
            text=text_value,
            engine=self.name,
            confidence=round(mean_confidence, 2) if mean_confidence is not None else None,
            language=language,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


class OcrCache:
    """Content-addressed OCR cache so re-runs never re-OCR the same image."""

    def __init__(self, path: Optional[str | Path]) -> None:
        self.path = Path(path).expanduser() if path else None
        self._entries: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self._entries = payload.get("entries", {})
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable OCR cache %s: %s", self.path, exc)

    @staticmethod
    def key(sha256: str, engine: str, language: str) -> str:
        return f"{sha256}:{engine}:{language}"

    def get(self, key: str) -> Optional[OcrResult]:
        with self._lock:
            entry = self._entries.get(key)
        return OcrResult.from_dict(entry) if entry else None

    def put(self, key: str, result: OcrResult) -> None:
        with self._lock:
            self._entries[key] = result.to_dict()
            self._dirty = True

    def save(self) -> None:
        if not self.path or not self._dirty:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"version": 1, "entries": self._entries}, ensure_ascii=False, indent=0),
                encoding="utf-8",
            )
            self._dirty = False


def create_ocr_engine(config: OcrConfig) -> OcrEngine:
    """Instantiate the engine named by ``ocr.engine``."""
    if not config.enabled:
        return NullOcrEngine()
    engine = (config.engine or "tesseract").lower()
    if engine in ("none", "null"):
        return NullOcrEngine()
    if engine == "tesseract":
        return TesseractOcrEngine(
            languages=config.languages,
            timeout_seconds=config.timeout_seconds,
            min_confidence=config.min_confidence,
            tesseract_cmd=config.tesseract_cmd,
        )
    raise ValueError(
        f"Unknown OCR engine {config.engine!r}; use 'tesseract', 'null', or pass a "
        "custom engine to OneNoteParser(ocr_engine=...)"
    )
