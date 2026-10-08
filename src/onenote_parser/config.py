"""Layered configuration.

Precedence (lowest to highest)::

    dataclass defaults  <  config file (YAML/JSON)  <  environment  <  explicit kwargs

Every knob is a plain dataclass field so the config object is inspectable,
copyable and diffable.  ``ParserConfig.validate()`` raises
:class:`~onenote_parser.errors.ConfigurationError` with an actionable message
rather than failing deep inside a crawl.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from .errors import ConfigurationError
from .models import parse_graph_datetime

__all__ = [
    "DEFAULT_TOKEN_CACHE_PATH",
    "read_env_file",
    "AuthConfig",
    "GraphConfig",
    "ContentConfig",
    "ImageConfig",
    "OcrConfig",
    "AttachmentConfig",
    "FilterConfig",
    "StateConfig",
    "ExportConfig",
    "RuntimeConfig",
    "ParserConfig",
    "ENV_PREFIX",
]

ENV_PREFIX = "ONENOTE_PARSER__"

DEFAULT_SCOPES = ("Notes.Read.All", "offline_access")

#: Default MSAL token cache location, shared by the ``login`` command and every
#: subsequent run so an interactive sign-in only has to happen once.
DEFAULT_TOKEN_CACHE_PATH = "~/.onenote-parser/token-cache.json"

#: Curated, friendly environment aliases -> dotted config path.
ENV_ALIASES: dict[str, str] = {
    "ONENOTE_ACCESS_TOKEN": "auth.access_token",
    "ONENOTE_TENANT_ID": "auth.tenant_id",
    "ONENOTE_CLIENT_ID": "auth.client_id",
    "ONENOTE_CLIENT_SECRET": "auth.client_secret",
    "ONENOTE_REFRESH_TOKEN": "auth.refresh_token",
    "ONENOTE_AUTH_MODE": "auth.mode",
    "ONENOTE_TOKEN_CACHE": "auth.token_cache_path",
    "ONENOTE_TARGET": "graph.target",
    "ONENOTE_OUTPUT_DIR": "export.output_dir",
    "ONENOTE_STATE_PATH": "state.path",
    "ONENOTE_OCR_ENABLED": "ocr.enabled",
    "ONENOTE_LOG_LEVEL": "runtime.log_level",
}


def read_env_file(path: str | Path) -> dict[str, str]:
    """Parse a ``.env`` file into a mapping.

    Deliberately small and dependency-free.  Supports ``KEY=value``, an optional
    ``export`` prefix, ``#`` comments, blank lines, and single- or double-quoted
    values.  It does **not** do variable interpolation -- a literal ``$`` in a
    client secret must stay literal.

    Raises :class:`ConfigurationError` if the file is unreadable, but tolerates
    malformed lines by skipping them, so one bad line cannot block a run.
    """
    file_path = Path(path).expanduser()
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(
            f"Could not read env file {file_path}: {exc}", details={"path": str(file_path)}
        ) from exc

    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return tuple(str(v) for v in value)


@dataclass
class AuthConfig:
    """How to obtain a Microsoft Graph bearer token.

    ``mode`` selects the token provider:

    ``device_code``
        Interactive sign-in for a human/service account; caches tokens on disk.
    ``refresh_token``
        Headless: a stored delegated refresh token plus a confidential client.
        This is the supported way to run the parser unattended.
    ``client_credentials``
        App-only.  Microsoft **retired app-only access to the OneNote API on
        2025-03-31**; kept for non-OneNote Graph calls and for tenants with a
        documented exception.  Validation warns loudly.
    ``static``
        A token supplied directly (tests, notebooks, external token brokers).
    """

    mode: str = "device_code"
    tenant_id: str = "common"
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    refresh_token: Optional[str] = None
    access_token: Optional[str] = None
    authority: Optional[str] = None
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    #: Where MSAL persists tokens between runs.  Shared by ``login`` and by
    #: every later command, so signing in once is enough; set to ``None`` to
    #: disable on-disk caching entirely.
    token_cache_path: Optional[str] = DEFAULT_TOKEN_CACHE_PATH
    # Refresh a token this many seconds before it actually expires.
    expiry_leeway_seconds: int = 300

    VALID_MODES = ("device_code", "refresh_token", "client_credentials", "static")

    @property
    def resolved_authority(self) -> str:
        return self.authority or f"https://login.microsoftonline.com/{self.tenant_id}"


@dataclass
class GraphConfig:
    """Microsoft Graph endpoint, throttling and retry behaviour."""

    base_url: str = "https://graph.microsoft.com"
    api_version: str = "v1.0"
    #: Whose OneNote to read: ``me``, ``users/{id|upn}``, ``groups/{id}``,
    #: ``sites/{id}``.  Anything else is used verbatim as a path prefix.
    target: str = "me"
    timeout_seconds: float = 60.0
    connect_timeout_seconds: float = 15.0
    max_retries: int = 5
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    backoff_multiplier: float = 2.0
    #: OneNote frequently returns 429 with no Retry-After header; this is the
    #: floor used in that case (see docs/onenote-parser.md).
    default_retry_after_seconds: float = 10.0
    #: Client-side rate limit.  Graph allows ~600 req/min/user for OneNote.
    requests_per_minute: int = 300
    page_size: int = 100
    max_page_fetch: int = 10_000
    user_agent: str = "onenote-parser/1.0 (+https://github.com/example/onenote-parser)"
    verify_tls: bool = True
    proxies: dict[str, str] = field(default_factory=dict)

    @property
    def api_root(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.api_version.strip('/')}"

    @property
    def target_prefix(self) -> str:
        """The Graph path segment that scopes ``/onenote`` calls."""
        target = self.target.strip("/")
        return target or "me"


@dataclass
class ContentConfig:
    """Page HTML retrieval and parsing options."""

    #: ``includeIDs=true`` gives every element a stable ``data-id`` used for
    #: block-level anchors and deep links.  Strongly recommended.
    include_ids: bool = True
    #: Ask Graph for InkML alongside HTML (multipart response) so handwriting is
    #: at least preserved as stroke data.
    include_ink_ml: bool = False
    preserve_unsupported_html: bool = True
    #: Maximum characters kept in ``ParsedPage.text``; 0 disables the cap.
    max_text_chars: int = 0
    #: Treat a ``<div>``/``<p>`` whose text matches these as a code block.
    detect_code_blocks: bool = True
    #: Collapse runs of whitespace inside text nodes.
    normalize_whitespace: bool = True
    #: Drop paragraphs that are empty after normalization.
    drop_empty_blocks: bool = True
    #: Heading detection also considers OneNote inline font-size styling.
    infer_headings_from_style: bool = True
    #: OneNote outlines are absolutely positioned; order top-level containers by
    #: their (top, left) coordinates so the reading order matches the page.
    order_by_position: bool = True
    #: Skip the OneNote-generated title block (title + creation date) so the
    #: title is not duplicated as page content.
    skip_title_block: bool = True


@dataclass
class ImageConfig:
    """Image download and storage."""

    download: bool = True
    directory: str = "resources/images"
    max_bytes: int = 25 * 1024 * 1024
    #: Store one file per unique sha256 and reuse it across pages.
    deduplicate: bool = True
    #: Skip images smaller than this many bytes (spacers, bullets, icons).
    min_bytes: int = 1024
    #: Prefer ``data-fullres-src`` over ``src`` when present.
    prefer_full_resolution: bool = True
    #: Reuse bytes already on disk from a previous run instead of re-downloading
    #: them.  Keyed by Graph resource id, which changes when an image is
    #: replaced, so this is safe and makes an interrupted run cheap to resume.
    reuse_downloaded: bool = True
    embed_base64: bool = False


@dataclass
class OcrConfig:
    """Optional OCR over downloaded images (handwriting/scans/screenshots)."""

    enabled: bool = False
    engine: str = "tesseract"
    languages: tuple[str, ...] = ("eng",)
    #: Skip tiny images; OCR on them is noise.
    min_width: int = 64
    min_height: int = 64
    max_bytes: int = 15 * 1024 * 1024
    timeout_seconds: float = 60.0
    #: Discard OCR output below this mean confidence (0-100). ``None`` keeps all.
    min_confidence: Optional[float] = None
    #: Cache OCR results keyed by image sha256.
    cache_path: Optional[str] = "resources/ocr-cache.json"
    #: OCR ink/handwriting images too (usually low quality, off by default).
    include_ink: bool = True
    tesseract_cmd: Optional[str] = None


@dataclass
class AttachmentConfig:
    """Embedded file / attachment download and text extraction."""

    download: bool = True
    directory: str = "resources/attachments"
    max_bytes: int = 50 * 1024 * 1024
    deduplicate: bool = True
    extract_text: bool = True
    #: Reuse bytes already on disk from a previous run (see ImageConfig).
    reuse_downloaded: bool = True
    max_extracted_chars: int = 200_000
    #: Also read PDF annotation text (Bluebeam/Acrobat markups, form field
    #: values).  Markup-heavy engineering PDFs often carry all of their text in
    #: annotations, leaving the page content stream empty.
    pdf_include_annotations: bool = True
    #: Empty means "every extension we have an extractor for".
    allowed_extensions: tuple[str, ...] = ()
    denied_extensions: tuple[str, ...] = (".exe", ".dll", ".msi", ".bat", ".cmd", ".scr")


@dataclass
class FilterConfig:
    """Which parts of the notebook set to process."""

    notebook_ids: tuple[str, ...] = ()
    notebook_names: tuple[str, ...] = ()
    section_ids: tuple[str, ...] = ()
    section_names: tuple[str, ...] = ()
    page_ids: tuple[str, ...] = ()
    exclude_notebook_names: tuple[str, ...] = ()
    exclude_section_names: tuple[str, ...] = ()
    #: Names above are matched as case-insensitive regex when True.
    use_regex: bool = False
    modified_since: Optional[datetime] = None
    max_pages: Optional[int] = None
    include_subpages: bool = True


@dataclass
class StateConfig:
    """Incremental-sync state store."""

    enabled: bool = True
    path: str = "state/onenote-state.json"
    #: ``incremental`` skips unchanged pages; ``full`` re-parses everything but
    #: still reports change status.
    mode: str = "incremental"
    #: Report pages present in state but absent now as DELETED.
    detect_deletions: bool = True
    #: Keep entries for pages that became inaccessible instead of dropping them.
    retain_inaccessible: bool = True

    VALID_MODES = ("incremental", "full")


@dataclass
class ExportConfig:
    """Normalized output."""

    formats: tuple[str, ...] = ("json",)
    output_dir: str = "output"
    pretty: bool = True
    #: Write one JSON file per page in addition to the bundle.
    per_page_files: bool = True
    #: Include the raw OneNote HTML alongside the normalized blocks.
    include_raw_html: bool = False
    #: Overwrite an existing output directory without complaining.
    overwrite: bool = True
    markdown_front_matter: bool = True
    #: Mirror the notebook hierarchy as directories in the Markdown export.
    markdown_mirror_hierarchy: bool = True

    VALID_FORMATS = ("json", "jsonl", "markdown")


@dataclass
class RuntimeConfig:
    """Process-level behaviour."""

    #: Threads used for page fetch/parse.  1 keeps ordering fully deterministic.
    concurrency: int = 4
    log_level: str = "INFO"
    log_json: bool = False
    #: Abort the run after this many consecutive page failures. 0 = never.
    fail_fast_after: int = 0
    #: Continue past individual page errors (recorded in ``ParseResult.errors``).
    continue_on_error: bool = True
    dry_run: bool = False


@dataclass
class ParserConfig:
    """Root configuration object."""

    auth: AuthConfig = field(default_factory=AuthConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    content: ContentConfig = field(default_factory=ContentConfig)
    images: ImageConfig = field(default_factory=ImageConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    attachments: AttachmentConfig = field(default_factory=AttachmentConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    state: StateConfig = field(default_factory=StateConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    # -- construction ----------------------------------------------------- #

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ParserConfig":
        """Build a config from a nested mapping, ignoring nothing silently."""
        config = cls()
        unknown: list[str] = []
        for section_name, values in (data or {}).items():
            if not hasattr(config, section_name):
                unknown.append(section_name)
                continue
            section = getattr(config, section_name)
            if not dataclasses.is_dataclass(section) or not isinstance(values, Mapping):
                unknown.append(section_name)
                continue
            valid = {f.name: f for f in dataclasses.fields(section)}
            for key, value in values.items():
                if key not in valid:
                    unknown.append(f"{section_name}.{key}")
                    continue
                setattr(section, key, _coerce(valid[key], value))
        if unknown:
            raise ConfigurationError(
                "Unknown configuration keys: " + ", ".join(sorted(unknown)),
                details={"unknown_keys": sorted(unknown)},
            )
        return config

    @classmethod
    def from_file(cls, path: str | Path) -> "ParserConfig":
        """Load YAML (if PyYAML is installed) or JSON."""
        file_path = Path(path)
        if not file_path.exists():
            raise ConfigurationError(f"Config file not found: {file_path}")
        text = file_path.read_text(encoding="utf-8")
        if file_path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore
            except ImportError as exc:  # pragma: no cover - env dependent
                raise ConfigurationError(
                    "YAML config requires PyYAML: pip install 'onenote-parser[yaml]'"
                ) from exc
            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text or "{}")
        if not isinstance(data, Mapping):
            raise ConfigurationError(f"Config file must contain a mapping: {file_path}")
        return cls.from_dict(data)

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> "ParserConfig":
        """Full precedence chain: file, then environment, then overrides."""
        config = cls.from_file(path) if path else cls()
        config.apply_env(env if env is not None else os.environ)
        if overrides:
            config.apply_overrides(overrides)
        return config

    # -- layering --------------------------------------------------------- #

    def apply_env(self, env: Mapping[str, str]) -> "ParserConfig":
        """Overlay environment variables.

        Two forms are supported: curated aliases (``ONENOTE_ACCESS_TOKEN``) and
        the generic ``ONENOTE_PARSER__<section>__<key>`` form.
        """
        for name, dotted in ENV_ALIASES.items():
            if name in env and env[name] != "":
                self.set_path(dotted, env[name])
        for name, value in env.items():
            if not name.startswith(ENV_PREFIX):
                continue
            remainder = name[len(ENV_PREFIX) :]
            parts = [p for p in remainder.split("__") if p]
            if len(parts) != 2:
                continue
            self.set_path(f"{parts[0].lower()}.{parts[1].lower()}", value)
        return self

    def apply_overrides(self, overrides: Mapping[str, Any]) -> "ParserConfig":
        """Apply ``{"section.key": value}`` or nested-mapping overrides."""
        for key, value in overrides.items():
            if isinstance(value, Mapping) and "." not in key:
                for sub_key, sub_value in value.items():
                    self.set_path(f"{key}.{sub_key}", sub_value)
            else:
                self.set_path(key, value)
        return self

    def set_path(self, dotted: str, value: Any) -> None:
        """Set ``section.key`` with type coercion driven by the dataclass field."""
        section_name, _, key = dotted.partition(".")
        if not key:
            raise ConfigurationError(f"Override must be 'section.key', got {dotted!r}")
        section = getattr(self, section_name, None)
        if section is None or not dataclasses.is_dataclass(section):
            raise ConfigurationError(f"Unknown configuration section {section_name!r}")
        fields = {f.name: f for f in dataclasses.fields(section)}
        if key not in fields:
            raise ConfigurationError(f"Unknown configuration key {dotted!r}")
        setattr(section, key, _coerce(fields[key], value))

    def to_dict(self, *, redact_secrets: bool = True) -> dict[str, Any]:
        """Nested plain-dict view, with credentials redacted by default."""
        out: dict[str, Any] = {}
        for section_field in dataclasses.fields(self):
            section = getattr(self, section_field.name)
            values: dict[str, Any] = {}
            for item in dataclasses.fields(section):
                value = getattr(section, item.name)
                if isinstance(value, tuple):
                    value = list(value)
                elif isinstance(value, datetime):
                    value = value.isoformat()
                if redact_secrets and item.name in _SECRET_FIELDS and value:
                    value = "***redacted***"
                values[item.name] = value
            out[section_field.name] = values
        return out

    def copy(self) -> "ParserConfig":
        return copy.deepcopy(self)

    # -- paths ------------------------------------------------------------ #

    @property
    def output_path(self) -> Path:
        return Path(self.export.output_dir).expanduser()

    def resolve_output(self, relative: str) -> Path:
        """Resolve a configured sub-path against the output directory."""
        candidate = Path(relative).expanduser()
        return candidate if candidate.is_absolute() else self.output_path / candidate

    # -- validation ------------------------------------------------------- #

    def validate(self) -> list[str]:
        """Raise on fatal problems; return a list of non-fatal warnings."""
        problems: list[str] = []
        warnings: list[str] = []

        if self.auth.mode not in AuthConfig.VALID_MODES:
            problems.append(
                f"auth.mode must be one of {AuthConfig.VALID_MODES}, got {self.auth.mode!r}"
            )
        if self.auth.mode == "static" and not self.auth.access_token:
            problems.append("auth.mode='static' requires auth.access_token (or ONENOTE_ACCESS_TOKEN)")
        if self.auth.mode in {"device_code", "refresh_token", "client_credentials"} and not self.auth.client_id:
            problems.append(f"auth.mode={self.auth.mode!r} requires auth.client_id")
        if self.auth.mode == "refresh_token" and not self.auth.refresh_token:
            problems.append("auth.mode='refresh_token' requires auth.refresh_token")
        if self.auth.mode == "client_credentials":
            if not self.auth.client_secret:
                problems.append("auth.mode='client_credentials' requires auth.client_secret")
            if self.auth.tenant_id in {"common", "organizations", "consumers"}:
                problems.append("auth.mode='client_credentials' requires a concrete auth.tenant_id")
            warnings.append(
                "Microsoft retired app-only (client credentials) access to the OneNote API on "
                "2025-03-31; use auth.mode='refresh_token' with a delegated service identity."
            )

        if self.graph.max_retries < 0:
            problems.append("graph.max_retries must be >= 0")
        if self.graph.requests_per_minute <= 0:
            problems.append("graph.requests_per_minute must be > 0")
        if self.graph.requests_per_minute > 600:
            warnings.append(
                "graph.requests_per_minute > 600 exceeds the documented OneNote per-user "
                "limit and will cause sustained throttling."
            )
        if not 1 <= self.graph.page_size <= 100:
            problems.append("graph.page_size must be between 1 and 100")

        if self.state.mode not in StateConfig.VALID_MODES:
            problems.append(f"state.mode must be one of {StateConfig.VALID_MODES}")
        if self.state.mode == "incremental" and not self.state.enabled:
            warnings.append("state.mode='incremental' has no effect while state.enabled is false")

        unknown_formats = [f for f in self.export.formats if f not in ExportConfig.VALID_FORMATS]
        if unknown_formats:
            problems.append(
                f"export.formats contains unknown formats {unknown_formats}; "
                f"valid: {list(ExportConfig.VALID_FORMATS)}"
            )
        if not self.export.formats:
            warnings.append("export.formats is empty; the run will parse but write nothing")

        if self.runtime.concurrency < 1:
            problems.append("runtime.concurrency must be >= 1")
        if self.runtime.concurrency > 16:
            warnings.append("runtime.concurrency > 16 will almost certainly trigger Graph throttling")

        if self.ocr.enabled and not self.images.download:
            problems.append("ocr.enabled requires images.download=true")
        if self.attachments.extract_text and not self.attachments.download:
            problems.append("attachments.extract_text requires attachments.download=true")

        if self.filters.use_regex:
            for group in (
                self.filters.notebook_names,
                self.filters.section_names,
                self.filters.exclude_notebook_names,
                self.filters.exclude_section_names,
            ):
                for pattern in group:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        problems.append(f"Invalid filter regex {pattern!r}: {exc}")

        if problems:
            raise ConfigurationError(
                "Invalid configuration:\n  - " + "\n  - ".join(problems),
                details={"problems": problems},
            )
        return warnings


_SECRET_FIELDS = {"client_secret", "refresh_token", "access_token"}


def _coerce(field_def: dataclasses.Field, value: Any) -> Any:
    """Coerce a raw (often string) value to the dataclass field's type."""
    annotation = field_def.type
    text_annotation = annotation if isinstance(annotation, str) else str(annotation)
    if value is None:
        return None
    if "bool" in text_annotation and "Optional" not in text_annotation:
        return _as_bool(value)
    if "tuple[str" in text_annotation:
        return _as_tuple(value)
    if "datetime" in text_annotation:
        if isinstance(value, datetime):
            return value
        parsed = parse_graph_datetime(value)
        if parsed is None:
            raise ConfigurationError(f"Cannot parse timestamp for {field_def.name}: {value!r}")
        return parsed
    if "int" in text_annotation and "Optional" in text_annotation:
        return None if value in ("", None) else int(value)
    if "float" in text_annotation and "Optional" in text_annotation:
        return None if value in ("", None) else float(value)
    if "dict" in text_annotation:
        if isinstance(value, str):
            return json.loads(value) if value.strip() else {}
        return dict(value)
    if "int" in text_annotation:
        return int(value)
    if "float" in text_annotation:
        return float(value)
    if "bool" in text_annotation:
        return _as_bool(value)
    return value
