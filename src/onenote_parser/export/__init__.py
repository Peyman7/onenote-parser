"""Normalized output writers.

All exporters share the :class:`Exporter` protocol, so adding a target (Parquet,
a database, a search index) means implementing one method.  Formats are
documented in ``docs/output-schema.md`` and versioned by
:data:`onenote_parser.models.SCHEMA_VERSION`.
"""

from .base import Exporter, ExportManifest, create_exporters, slugify
from .json_export import JsonExporter
from .jsonl_export import JsonlExporter
from .markdown_export import MarkdownExporter

__all__ = [
    "Exporter",
    "ExportManifest",
    "JsonExporter",
    "JsonlExporter",
    "MarkdownExporter",
    "create_exporters",
    "slugify",
]
