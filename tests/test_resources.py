"""Image download/OCR and attachment extraction."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from onenote_parser.config import AttachmentConfig, ImageConfig, OcrConfig
from onenote_parser.errors import ResourceError
from onenote_parser.models import FileBlock, ImageBlock, OcrResult
from onenote_parser.resources import (
    AttachmentProcessor,
    CallableOcrEngine,
    ImageProcessor,
    NullOcrEngine,
    OcrCache,
    ResourceStore,
    extractor_for,
    register_extractor,
)
from onenote_parser.resources.attachments import (
    DocxTextExtractor,
    PlainTextExtractor,
    PptxTextExtractor,
    XlsxTextExtractor,
)
from onenote_parser.resources.images import sniff_mime
from onenote_parser.resources.store import safe_filename

from conftest import PNG_BYTES, make_docx


class StubClient:
    """Serves canned bytes for resource URLs and records the calls."""

    def __init__(self, payloads=None, error=None):
        self.payloads = payloads or {}
        self.error = error
        self.calls = []

    def get_bytes(self, url, *, accept="*/*"):
        self.calls.append(url)
        if self.error:
            raise self.error
        data, mime = self.payloads.get(url, (b"", ""))
        return data, {"content-type": mime}


# -- store ------------------------------------------------------------------- #


def test_store_deduplicates_identical_content(tmp_path):
    store = ResourceStore(tmp_path / "images")
    first, digest, reused_a = store.store(PNG_BYTES, filename="a.png", mime_type="image/png")
    second, _, reused_b = store.store(PNG_BYTES, filename="b.png", mime_type="image/png")
    assert reused_a is False and reused_b is True
    assert first == second
    assert len(list((tmp_path / "images").iterdir())) == 1
    assert first.name.startswith(digest[:16])


def test_store_leaves_no_partial_files(tmp_path):
    store = ResourceStore(tmp_path / "r")
    path, _, _ = store.store(b"data", filename="x.bin")
    assert path.read_bytes() == b"data"
    assert not list(path.parent.glob("*.part"))


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\temp\\report.pdf", "report.pdf"),
        ("a b*c?.png", "a_b_c_.png"),
        ("", "resource"),
    ],
)
def test_safe_filename_blocks_traversal_and_unsafe_characters(raw, expected):
    assert safe_filename(raw) == expected


def test_store_infers_an_extension_from_the_mime_type(tmp_path):
    store = ResourceStore(tmp_path / "r")
    path, _, _ = store.store(PNG_BYTES, filename="noext", mime_type="image/png")
    assert path.suffix == ".png"


# -- images ------------------------------------------------------------------ #


def image_processor(tmp_path, client, config=None, **kwargs):
    return ImageProcessor(client, config or ImageConfig(min_bytes=10),
                          ResourceStore(tmp_path / "images"), output_root=tmp_path, **kwargs)


def test_image_is_downloaded_hashed_and_stored(tmp_path):
    url = "https://graph/resources/r1/$value"
    client = StubClient({url: (PNG_BYTES, "image/png")})
    block = ImageBlock(resource_id="r1", source_url=url)
    image_processor(tmp_path, client).process(block)

    assert block.downloaded is True
    assert block.mime_type == "image/png"
    assert block.byte_size == len(PNG_BYTES)
    assert len(block.sha256) == 64
    assert block.file_path == f"images/{block.sha256[:16]}-r1.png"
    assert (tmp_path / block.file_path).exists()


def test_full_resolution_url_is_preferred(tmp_path):
    small, large = "https://graph/small", "https://graph/large"
    client = StubClient({large: (PNG_BYTES, "image/png")})
    block = ImageBlock(source_url=small, full_resource_url=large)
    image_processor(tmp_path, client).process(block)
    assert client.calls == [large]

    client = StubClient({small: (PNG_BYTES, "image/png")})
    block = ImageBlock(source_url=small, full_resource_url=large)
    image_processor(tmp_path, client, ImageConfig(min_bytes=10,
                                                  prefer_full_resolution=False)).process(block)
    assert client.calls == [small]


def test_download_failure_is_recorded_on_the_block_not_raised(tmp_path):
    client = StubClient(error=RuntimeError("boom"))
    block = ImageBlock(source_url="https://graph/x")
    image_processor(tmp_path, client).process(block)
    assert block.downloaded is False and "boom" in block.error


def test_oversized_and_tiny_images_are_skipped(tmp_path):
    url = "https://graph/x"
    client = StubClient({url: (PNG_BYTES, "image/png")})
    block = ImageBlock(source_url=url)
    image_processor(tmp_path, client, ImageConfig(max_bytes=10)).process(block)
    assert block.error.startswith("too_large") and block.downloaded is False

    client = StubClient({url: (b"tiny", "image/png")})
    block = ImageBlock(source_url=url)
    image_processor(tmp_path, client, ImageConfig(min_bytes=1024)).process(block)
    assert block.error.startswith("below_min_bytes")


def test_mime_is_sniffed_from_magic_bytes_when_headers_lie(tmp_path):
    url = "https://graph/x"
    client = StubClient({url: (PNG_BYTES, "application/octet-stream")})
    block = ImageBlock(source_url=url, mime_type="image/jpeg")
    image_processor(tmp_path, client).process(block)
    assert block.mime_type == "image/png"


@pytest.mark.parametrize(
    "data,expected",
    [
        (PNG_BYTES, "image/png"),
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (b"GIF89a...", "image/gif"),
        (b"RIFF1234WEBPxx", "image/webp"),
        (b"<svg xmlns='x'></svg>", "image/svg+xml"),
        (b"not an image", None),
    ],
)
def test_sniff_mime(data, expected):
    assert sniff_mime(data) == expected


def test_download_can_be_disabled(tmp_path):
    client = StubClient()
    block = ImageBlock(source_url="https://graph/x")
    image_processor(tmp_path, client, ImageConfig(download=False)).process(block)
    assert client.calls == [] and block.downloaded is False


# -- OCR --------------------------------------------------------------------- #


def test_ocr_runs_when_enabled_and_populates_the_block(tmp_path):
    url = "https://graph/x"
    client = StubClient({url: (PNG_BYTES, "image/png")})
    engine = CallableOcrEngine(lambda data: "4.5 mm/s", name="stub")
    processor = image_processor(tmp_path, client, ocr_engine=engine,
                                ocr_config=OcrConfig(enabled=True, min_width=1, min_height=1))
    block = ImageBlock(source_url=url, width=100, height=100)
    processor.process(block)
    assert block.ocr.text == "4.5 mm/s" and block.ocr.engine == "stub"
    assert processor.ocr_count == 1


def test_ocr_is_skipped_when_disabled(tmp_path):
    url = "https://graph/x"
    client = StubClient({url: (PNG_BYTES, "image/png")})
    processor = image_processor(tmp_path, client,
                                ocr_engine=CallableOcrEngine(lambda d: "text"),
                                ocr_config=OcrConfig(enabled=False))
    block = ImageBlock(source_url=url)
    processor.process(block)
    assert block.ocr is None


def test_ocr_skips_images_below_the_size_threshold(tmp_path):
    url = "https://graph/x"
    client = StubClient({url: (PNG_BYTES, "image/png")})
    processor = image_processor(tmp_path, client,
                                ocr_engine=CallableOcrEngine(lambda d: "text"),
                                ocr_config=OcrConfig(enabled=True, min_width=500, min_height=500))
    block = ImageBlock(source_url=url, width=32, height=32)
    processor.process(block)
    assert block.ocr is None


def test_ocr_can_skip_ink_images(tmp_path):
    url = "https://graph/x"
    client = StubClient({url: (PNG_BYTES, "image/png")})
    processor = image_processor(
        tmp_path, client, ocr_engine=CallableOcrEngine(lambda d: "text"),
        ocr_config=OcrConfig(enabled=True, include_ink=False, min_width=1, min_height=1),
    )
    block = ImageBlock(source_url=url, is_ink=True, width=100, height=100)
    processor.process(block)
    assert block.ocr is None


def test_ocr_engine_failures_are_captured_not_raised():
    def explode(_data):
        raise RuntimeError("engine down")

    result = CallableOcrEngine(explode, name="stub").recognize(b"x")
    assert result.text == "" and "engine down" in result.error


def test_ocr_cache_avoids_recomputation(tmp_path):
    url = "https://graph/x"
    calls = []

    def engine_fn(data):
        calls.append(1)
        return "cached text"

    cache = OcrCache(tmp_path / "ocr.json")
    config = OcrConfig(enabled=True, min_width=1, min_height=1)
    for _ in range(2):
        client = StubClient({url: (PNG_BYTES, "image/png")})
        processor = ImageProcessor(
            client, ImageConfig(min_bytes=10), ResourceStore(tmp_path / "images"),
            ocr_engine=CallableOcrEngine(engine_fn, name="stub"), ocr_config=config,
            ocr_cache=cache, output_root=tmp_path,
        )
        block = ImageBlock(source_url=url, width=100, height=100)
        processor.process(block)
        assert block.ocr.text == "cached text"
    assert len(calls) == 1

    cache.save()
    assert json.loads((tmp_path / "ocr.json").read_text())["entries"]


def test_null_ocr_engine_returns_empty_text():
    assert NullOcrEngine().recognize(b"x").text == ""


# -- attachments ------------------------------------------------------------- #


def attachment_processor(tmp_path, client, config=None):
    return AttachmentProcessor(client, config or AttachmentConfig(),
                               ResourceStore(tmp_path / "attachments"), output_root=tmp_path)


def test_attachment_is_downloaded_and_text_extracted(tmp_path):
    url = "https://graph/resources/doc/$value"
    data = make_docx(["Commissioning report", "All checks passed."])
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    client = StubClient({url: (data, mime)})
    block = FileBlock(source_url=url, file_name="report.docx", mime_type=mime)
    attachment_processor(tmp_path, client).process(block)

    assert block.downloaded and block.extraction_status == "ok"
    assert block.extractor == "docx"
    assert "Commissioning report" in block.extracted_text
    assert "All checks passed." in block.extracted_text
    assert (tmp_path / block.file_path).exists()


def test_denied_extensions_are_never_downloaded(tmp_path):
    client = StubClient()
    block = FileBlock(source_url="https://graph/x", file_name="payload.exe")
    attachment_processor(tmp_path, client).process(block)
    assert client.calls == []
    assert block.error == "extension_denied:.exe"


def test_allowlist_restricts_downloads(tmp_path):
    client = StubClient()
    config = AttachmentConfig(allowed_extensions=(".pdf",))
    block = FileBlock(source_url="https://graph/x", file_name="notes.txt")
    attachment_processor(tmp_path, client, config).process(block)
    assert client.calls == [] and block.error == "extension_not_allowed:.txt"


def test_extracted_text_is_truncated_at_the_configured_limit(tmp_path):
    url = "https://graph/x"
    client = StubClient({url: (b"y" * 5000, "text/plain")})
    config = AttachmentConfig(max_extracted_chars=100)
    block = FileBlock(source_url=url, file_name="big.txt")
    attachment_processor(tmp_path, client, config).process(block)
    assert block.extraction_status == "truncated" and len(block.extracted_text) == 100


def test_unsupported_format_is_stored_but_flagged(tmp_path):
    url = "https://graph/x"
    client = StubClient({url: (b"\x00\x01binary", "application/octet-stream")})
    block = FileBlock(source_url=url, file_name="model.step")
    attachment_processor(tmp_path, client).process(block)
    assert block.downloaded is True and block.extraction_status == "unsupported"


def test_download_failure_is_recorded(tmp_path):
    client = StubClient(error=RuntimeError("network gone"))
    block = FileBlock(source_url="https://graph/x", file_name="a.txt")
    attachment_processor(tmp_path, client).process(block)
    assert block.extraction_status == "failed" and "network gone" in block.error


def test_json_attachments_are_pretty_printed():
    text = PlainTextExtractor().extract(b'{"b":1,"a":2}', filename="x.json")
    assert text.splitlines()[0] == "{"


def test_pptx_extraction_labels_slides():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:a="x"><a:p><a:r><a:t>Agenda</a:t></a:r></a:p></p:sld>',
        )
    text = PptxTextExtractor().extract(buffer.getvalue())
    assert "## Slide 1" in text and "Agenda" in text


def test_xlsx_falls_back_to_shared_strings_without_openpyxl(monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", "<sst><si><t>Vibration</t></si></sst>")

    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "openpyxl":
            raise ImportError("no openpyxl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert "Vibration" in XlsxTextExtractor().extract(buffer.getvalue())


def test_corrupt_office_package_raises_resource_error():
    with pytest.raises(ResourceError):
        DocxTextExtractor().extract(b"not a zip")


def test_extractor_lookup_prefers_extension_then_mime():
    assert extractor_for("a.pdf", "text/plain").name == "pdf"
    assert extractor_for("noextension", "text/csv").name == "plaintext"
    assert extractor_for("a.unknown", "application/x-unknown") is None


def test_custom_extractors_can_be_registered():
    class StepExtractor:
        name = "step"
        extensions = (".step",)
        mime_types = ("application/step",)

        def extract(self, data, *, filename=None):
            return "ISO-10303"

    register_extractor(StepExtractor())
    try:
        assert extractor_for("part.step", None).extract(b"") == "ISO-10303"
    finally:
        from onenote_parser.resources import attachments

        attachments._REGISTRY = [e for e in attachments._REGISTRY if e.name != "step"]


# -- OCR engine construction ------------------------------------------------- #


def test_create_ocr_engine_selects_by_name():
    from onenote_parser.resources.ocr import (
        NullOcrEngine as Null,
        TesseractOcrEngine,
        create_ocr_engine,
    )

    assert isinstance(create_ocr_engine(OcrConfig(enabled=False)), Null)
    assert isinstance(create_ocr_engine(OcrConfig(enabled=True, engine="null")), Null)
    assert isinstance(create_ocr_engine(OcrConfig(enabled=True)), TesseractOcrEngine)
    with pytest.raises(ValueError):
        create_ocr_engine(OcrConfig(enabled=True, engine="cloud-vision"))


def test_tesseract_engine_reports_missing_dependencies_instead_of_crashing():
    """A missing native binary or Python package must degrade, not fail the run."""
    from onenote_parser.resources.ocr import TesseractOcrEngine

    result = TesseractOcrEngine(languages=("eng",)).recognize(b"not a real image")
    assert result.text == ""
    assert result.error  # either "dependencies missing" or an image-decode error
    assert result.engine == "tesseract"


def test_ocr_cache_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "ocr.json"
    path.write_text("{ broken")
    cache = OcrCache(path)
    assert cache.get("anything") is None
    cache.put("k", OcrResult(text="v", engine="stub"))
    cache.save()
    assert OcrCache(path).get("k").text == "v"


def test_pdf_extraction_reports_unreadable_input():
    from onenote_parser.resources.attachments import PdfTextExtractor

    with pytest.raises(Exception):
        PdfTextExtractor().extract(b"definitely not a pdf")


def test_processors_repair_legacy_resource_urls(tmp_path):
    """Regression: site-hosted notebooks embed unusable siteCollections URLs."""
    broken = ("https://graph.microsoft.com/v1.0/siteCollections/host,a,b"
              "/onenote/resources/res-1/$value")
    fixed = "https://graph.microsoft.com/v1.0/sites/host,a,b/onenote/resources/res-1/$value"

    class RepairingClient(StubClient):
        def resource_url(self, url):
            return fixed if url == broken else url

    client = RepairingClient({fixed: (PNG_BYTES, "image/png")})
    block = ImageBlock(resource_id="res-1", source_url=broken)
    image_processor(tmp_path, client).process(block)
    assert client.calls == [fixed]
    assert block.downloaded is True
    assert block.source_url == broken  # provenance preserved

    client = RepairingClient({fixed: (b"text payload", "text/plain")})
    attachment = FileBlock(resource_id="res-1", source_url=broken, file_name="notes.txt")
    attachment_processor(tmp_path, client).process(attachment)
    assert attachment.downloaded is True and attachment.extracted_text == "text payload"


def test_processors_work_with_a_client_that_lacks_resource_url(tmp_path):
    url = "https://graph/resources/r1/$value"
    client = StubClient({url: (PNG_BYTES, "image/png")})   # no resource_url method
    block = ImageBlock(source_url=url)
    image_processor(tmp_path, client).process(block)
    assert block.downloaded is True


# -- PDF annotations --------------------------------------------------------- #


def make_annotated_pdf(page_text: str | None, annotations: list[str]) -> bytes:
    """A real one-page PDF whose text lives in FreeText annotations."""
    from pypdf import PdfWriter
    from pypdf.annotations import FreeText

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    for index, value in enumerate(annotations):
        writer.add_annotation(
            page_number=0,
            annotation=FreeText(text=value, rect=(50, 700 - index * 40, 500, 730 - index * 40)),
        )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_pdf_text_is_recovered_from_annotations():
    """Bluebeam/Acrobat markups leave the content stream empty (real corpus case)."""
    from onenote_parser.resources.attachments import PdfTextExtractor

    data = make_annotated_pdf(None, ["Developer Services QC Checklist", "Drafter"])
    text = PdfTextExtractor().extract(data)
    assert "Developer Services QC Checklist" in text
    assert "Drafter" in text


def test_pdf_annotations_can_be_disabled():
    from onenote_parser.resources.attachments import PdfTextExtractor

    data = make_annotated_pdf(None, ["Only in an annotation"])
    assert PdfTextExtractor(include_annotations=False).extract(data).strip() == ""


def test_pdf_annotation_carriage_returns_become_newlines():
    from onenote_parser.resources.attachments import PdfTextExtractor

    data = make_annotated_pdf(None, ["line one\rline two"])
    text = PdfTextExtractor().extract(data)
    assert "\r" not in text
    assert "line one\nline two" in text


def test_pdf_annotation_text_is_deduplicated():
    from onenote_parser.resources.attachments import PdfTextExtractor

    data = make_annotated_pdf(None, ["Repeated note", "Repeated note", "Distinct note"])
    text = PdfTextExtractor().extract(data)
    assert text.count("Repeated note") == 1
    assert "Distinct note" in text


def test_attachment_processor_honours_the_annotation_setting(tmp_path):
    url = "https://graph/x"
    data = make_annotated_pdf(None, ["Checklist item"])
    for enabled, expected in ((True, "ok"), (False, "empty")):
        client = StubClient({url: (data, "application/pdf")})
        config = AttachmentConfig(pdf_include_annotations=enabled)
        block = FileBlock(source_url=url, file_name="checklist.pdf")
        attachment_processor(tmp_path, client, config).process(block)
        assert block.extraction_status == expected
        if enabled:
            assert "Checklist item" in block.extracted_text


# -- resource reuse across runs ---------------------------------------------- #


def test_second_run_reuses_downloaded_images_without_refetching(tmp_path):
    """Throttled/interrupted runs must not re-download bytes already on disk."""
    url = "https://graph/resources/r1/$value"
    index = tmp_path / "images" / ".index.json"

    def build():
        return ResourceStore(tmp_path / "images", index_path=index)

    client = StubClient({url: (PNG_BYTES, "image/png")})
    first = ImageProcessor(client, ImageConfig(min_bytes=10), build(), output_root=tmp_path)
    block = ImageBlock(resource_id="r1", source_url=url)
    first.process(block)
    first.store.save_index()
    assert client.calls == [url] and block.downloaded

    client2 = StubClient()          # would raise KeyError-ish empty bytes if called
    second = ImageProcessor(client2, ImageConfig(min_bytes=10), build(), output_root=tmp_path)
    block2 = ImageBlock(resource_id="r1", source_url=url)
    second.process(block2)
    assert client2.calls == []                       # no network at all
    assert block2.downloaded is True
    assert block2.file_path == block.file_path
    assert block2.sha256 == block.sha256
    assert block2.byte_size == len(PNG_BYTES)


def test_reuse_can_be_disabled(tmp_path):
    url = "https://graph/resources/r1/$value"
    index = tmp_path / "images" / ".index.json"
    store = ResourceStore(tmp_path / "images", index_path=index)
    client = StubClient({url: (PNG_BYTES, "image/png")})
    ImageProcessor(client, ImageConfig(min_bytes=10), store,
                   output_root=tmp_path).process(ImageBlock(resource_id="r1", source_url=url))
    store.save_index()

    client2 = StubClient({url: (PNG_BYTES, "image/png")})
    ImageProcessor(client2, ImageConfig(min_bytes=10, reuse_downloaded=False),
                   ResourceStore(tmp_path / "images", index_path=index),
                   output_root=tmp_path).process(ImageBlock(resource_id="r1", source_url=url))
    assert client2.calls == [url]                    # re-downloaded on purpose


def test_reuse_falls_back_to_download_when_the_file_was_deleted(tmp_path):
    url = "https://graph/resources/r1/$value"
    index = tmp_path / "images" / ".index.json"
    store = ResourceStore(tmp_path / "images", index_path=index)
    client = StubClient({url: (PNG_BYTES, "image/png")})
    block = ImageBlock(resource_id="r1", source_url=url)
    ImageProcessor(client, ImageConfig(min_bytes=10), store, output_root=tmp_path).process(block)
    store.save_index()
    (tmp_path / block.file_path).unlink()            # cache file removed

    client2 = StubClient({url: (PNG_BYTES, "image/png")})
    ImageProcessor(client2, ImageConfig(min_bytes=10),
                   ResourceStore(tmp_path / "images", index_path=index),
                   output_root=tmp_path).process(ImageBlock(resource_id="r1", source_url=url))
    assert client2.calls == [url]


def test_reused_attachment_still_extracts_text(tmp_path):
    url = "https://graph/resources/d1/$value"
    index = tmp_path / "attachments" / ".index.json"
    data = make_docx(["Cached report body"])
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    store = ResourceStore(tmp_path / "attachments", index_path=index)
    client = StubClient({url: (data, mime)})
    AttachmentProcessor(client, AttachmentConfig(), store, output_root=tmp_path).process(
        FileBlock(resource_id="d1", source_url=url, file_name="r.docx", mime_type=mime))
    store.save_index()

    client2 = StubClient()
    block = FileBlock(resource_id="d1", source_url=url, file_name="r.docx", mime_type=mime)
    AttachmentProcessor(client2, AttachmentConfig(),
                        ResourceStore(tmp_path / "attachments", index_path=index),
                        output_root=tmp_path).process(block)
    assert client2.calls == []
    assert block.extraction_status == "ok"
    assert "Cached report body" in block.extracted_text


def test_reused_image_still_runs_ocr(tmp_path):
    url = "https://graph/resources/r1/$value"
    index = tmp_path / "images" / ".index.json"
    store = ResourceStore(tmp_path / "images", index_path=index)
    StubClient({url: (PNG_BYTES, "image/png")})
    ImageProcessor(StubClient({url: (PNG_BYTES, "image/png")}), ImageConfig(min_bytes=10),
                   store, output_root=tmp_path).process(
        ImageBlock(resource_id="r1", source_url=url))
    store.save_index()

    block = ImageBlock(resource_id="r1", source_url=url, width=100, height=100)
    processor = ImageProcessor(
        StubClient(), ImageConfig(min_bytes=10),
        ResourceStore(tmp_path / "images", index_path=index),
        ocr_engine=CallableOcrEngine(lambda d: "cached ocr", name="stub"),
        ocr_config=OcrConfig(enabled=True, min_width=1, min_height=1), output_root=tmp_path)
    processor.process(block)
    assert block.ocr.text == "cached ocr"


def test_store_without_an_index_never_reuses(tmp_path):
    store = ResourceStore(tmp_path / "images")          # no index_path
    store.remember("r1", {"file_path": "x"})
    assert store.lookup("r1") is None
    store.save_index()                                   # no-op, no crash


def test_corrupt_resource_index_is_ignored(tmp_path):
    index = tmp_path / "images" / ".index.json"
    index.parent.mkdir(parents=True)
    index.write_text("{ not json")
    store = ResourceStore(tmp_path / "images", index_path=index)
    assert store.lookup("r1") is None
