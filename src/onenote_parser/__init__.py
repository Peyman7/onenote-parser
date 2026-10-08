"""onenote-parser - extract, normalize and export Microsoft OneNote notebooks.

The package turns a OneNote notebook hierarchy reached through Microsoft Graph
into a stable, documented, JSON-serializable content model.  Its responsibility
ends at export; the output is designed to feed RAG pipelines, search indexes,
migrations, analytics and knowledge-management systems without any of them
having to understand OneNote HTML.

Quick start::

    from onenote_parser import OneNoteParser, ParserConfig

    config = ParserConfig.load("config.yaml")
    with OneNoteParser(config) as parser:
        result = parser.run()

See ``docs/onenote-parser.md`` for the architecture and
``docs/output-schema.md`` for the output contract.
"""

from .config import (
    AttachmentConfig,
    AuthConfig,
    ContentConfig,
    ExportConfig,
    FilterConfig,
    GraphConfig,
    ImageConfig,
    OcrConfig,
    ParserConfig,
    RuntimeConfig,
    StateConfig,
)
from .discovery import NotebookCrawler
from .errors import (
    AccessDeniedError,
    AuthenticationError,
    ConfigurationError,
    ExportError,
    GraphError,
    GraphHTTPError,
    NotFoundError,
    OneNoteParserError,
    ParseError,
    ResourceError,
    StateError,
    ThrottledError,
    TransportError,
)
from .graph import GraphClient, RequestsTransport, Response, Transport
from .logging_utils import configure_logging, get_logger
from .models import (
    SCHEMA_VERSION,
    Block,
    BlockType,
    ChangeSet,
    ChangeStatus,
    CodeBlock,
    DividerBlock,
    FileBlock,
    HeadingBlock,
    ImageBlock,
    LinkKind,
    ListBlock,
    ListItem,
    Mark,
    MarkType,
    MathBlock,
    NotebookNode,
    NotebookTree,
    OcrResult,
    PageChange,
    PageFlags,
    PageMetadata,
    PageRef,
    ParagraphBlock,
    ParsedPage,
    ParseResult,
    QuoteBlock,
    RichText,
    RunStats,
    SectionGroupNode,
    SectionNode,
    TableBlock,
    TableCell,
    TableRow,
    UnsupportedBlock,
)
from .parsing import OneNoteHtmlParser, render_markdown, render_text
from .pipeline import OneNoteParser
from .state import PageState, StateStore

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "SCHEMA_VERSION",
    # facade
    "OneNoteParser",
    "OneNoteHtmlParser",
    "NotebookCrawler",
    "GraphClient",
    "StateStore",
    "PageState",
    # config
    "ParserConfig",
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
    # transport
    "Transport",
    "RequestsTransport",
    "Response",
    # models
    "Block",
    "BlockType",
    "MarkType",
    "LinkKind",
    "ChangeStatus",
    "Mark",
    "RichText",
    "HeadingBlock",
    "ParagraphBlock",
    "ListBlock",
    "ListItem",
    "TableBlock",
    "TableRow",
    "TableCell",
    "ImageBlock",
    "FileBlock",
    "CodeBlock",
    "QuoteBlock",
    "MathBlock",
    "DividerBlock",
    "UnsupportedBlock",
    "OcrResult",
    "PageFlags",
    "PageMetadata",
    "PageRef",
    "SectionNode",
    "SectionGroupNode",
    "NotebookNode",
    "NotebookTree",
    "ParsedPage",
    "PageChange",
    "ChangeSet",
    "RunStats",
    "ParseResult",
    # helpers
    "render_text",
    "render_markdown",
    "configure_logging",
    "get_logger",
    # errors
    "OneNoteParserError",
    "ConfigurationError",
    "AuthenticationError",
    "GraphError",
    "GraphHTTPError",
    "TransportError",
    "NotFoundError",
    "AccessDeniedError",
    "ThrottledError",
    "ParseError",
    "ResourceError",
    "ExportError",
    "StateError",
]
