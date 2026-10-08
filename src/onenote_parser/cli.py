"""Command-line interface: ``onenote-parser <command> [options]``.

Commands
--------
``login``     interactive device-code sign-in; caches the token and can print a
              refresh token for unattended runs
``discover``  print the notebook hierarchy without fetching page content
``run``       the full pipeline: discover, parse, enrich, export
``page``      parse one page by id (or re-parse a local HTML file offline)
``status``    summarize the incremental-sync state file
``config``    print the effective, secret-redacted configuration
``schema``    print the output schema version and block catalogue
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from . import __version__
from .config import ExportConfig, ParserConfig, read_env_file
from .errors import AuthenticationError, ConfigurationError, OneNoteParserError
from .logging_utils import configure_logging, get_logger
from .models import SCHEMA_VERSION, BlockType, ChangeStatus, PageRef
from .state import StateStore

logger = get_logger("cli")

#: Auto-loaded from the working directory when present.
DEFAULT_ENV_FILE = ".env"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_AUTH = 3

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onenote-parser",
        description="Extract, normalize and export Microsoft OneNote notebooks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  onenote-parser login --client-id <app-id> --tenant-id <tenant>\n"
            "  onenote-parser discover --config config.yaml\n"
            "  onenote-parser run --config config.yaml --format json --format markdown\n"
            "  onenote-parser run --notebook 'Engineering' --max-pages 25 --dry-run\n"
            "  onenote-parser page 1-abc123 --out page.json\n"
            "  onenote-parser page --html-file cached.html --out page.json\n"
            "  onenote-parser status --state state/onenote-state.json\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"onenote-parser {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", help="YAML or JSON configuration file")
    common.add_argument("--env-file", metavar="PATH",
                        help="Load environment variables from this file "
                             f"(default: ./{DEFAULT_ENV_FILE} when present)")
    common.add_argument("--no-env-file", action="store_true",
                        help=f"Do not read ./{DEFAULT_ENV_FILE}")
    common.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                        help="Override a configuration value (repeatable)")
    common.add_argument("--target", help="Graph target: me, users/<id>, groups/<id>, sites/<id>")
    common.add_argument("--output", help="Output directory (default: output)")
    common.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    common.add_argument("--log-json", action="store_true", help="Emit structured JSON logs")

    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", parents=[common], help="Interactive device-code sign-in")
    login.add_argument("--client-id", help="Entra application (client) id")
    login.add_argument("--tenant-id", help="Directory (tenant) id, or 'common'")
    login.add_argument("--scope", action="append", default=[], help="Delegated scope (repeatable)")
    login.add_argument("--print-refresh-token", action="store_true",
                       help="Print the cached refresh token for unattended runs (secret!)")

    discover = sub.add_parser("discover", parents=[common], help="Print the notebook hierarchy")
    discover.add_argument("--recent", action="store_true",
                          help="List notebooks you have opened across every site/group "
                               "(finds team notebooks that 'me' does not return)")
    discover.add_argument("--json", action="store_true", help="Emit JSON instead of a tree")
    discover.add_argument("--out", help="Write the hierarchy to this file")
    _add_filter_flags(discover)

    run = sub.add_parser("run", parents=[common], help="Run the full extraction pipeline")
    run.add_argument("--format", action="append", default=[],
                     choices=list(ExportConfig.VALID_FORMATS), help="Export format (repeatable)")
    run.add_argument("--state", help="Path to the incremental-sync state file")
    run.add_argument("--full", action="store_true", help="Re-parse every page (state.mode=full)")
    run.add_argument("--no-state", action="store_true", help="Disable incremental state entirely")
    run.add_argument("--ocr", action="store_true", help="Enable OCR over downloaded images")
    run.add_argument("--no-resources", action="store_true",
                     help="Skip image and attachment downloads")
    run.add_argument("--concurrency", type=int, help="Worker threads for page fetch/parse")
    run.add_argument("--dry-run", action="store_true",
                     help="Discover and report only; fetch no page content")
    _add_filter_flags(run)

    page = sub.add_parser("page", parents=[common], help="Parse a single page")
    page.add_argument("page_id", nargs="?", help="OneNote page id")
    page.add_argument("--html-file", help="Parse a local HTML file instead of calling Graph")
    page.add_argument("--out", help="Write the normalized page JSON here (default: stdout)")
    page.add_argument("--markdown", action="store_true", help="Print Markdown instead of JSON")
    page.add_argument("--no-resources", action="store_true",
                      help="Skip image and attachment downloads")

    status = sub.add_parser("status", parents=[common], help="Summarize the sync state file")
    status.add_argument("--state", help="Path to the state file")
    status.add_argument("--json", action="store_true")

    sub.add_parser("config", parents=[common], help="Print the effective configuration")
    schema = sub.add_parser("schema", help="Print the output schema summary")
    schema.add_argument("--json", action="store_true")
    return parser


def _add_filter_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--notebook", action="append", default=[], metavar="NAME",
                        help="Only this notebook (repeatable)")
    parser.add_argument("--notebook-id", action="append", default=[], metavar="ID")
    parser.add_argument("--section", action="append", default=[], metavar="NAME",
                        help="Only this section (repeatable)")
    parser.add_argument("--exclude-notebook", action="append", default=[], metavar="NAME")
    parser.add_argument("--exclude-section", action="append", default=[], metavar="NAME")
    parser.add_argument("--regex-filters", action="store_true",
                        help="Treat name filters as case-insensitive regexes")
    parser.add_argument("--modified-since", metavar="ISO8601",
                        help="Only pages modified at or after this timestamp")
    parser.add_argument("--max-pages", type=int, help="Stop after this many pages")


# --------------------------------------------------------------------------- #
# Config assembly
# --------------------------------------------------------------------------- #


def _load_env_file(args: argparse.Namespace) -> Optional[Path]:
    """Merge a ``.env`` file into the process environment.

    Real environment variables always win, matching every other dotenv
    implementation: a value exported in the shell (or injected by a container
    orchestrator) must not be silently overridden by a file on disk.
    """
    if getattr(args, "no_env_file", False):
        return None
    explicit = getattr(args, "env_file", None)
    path = Path(explicit) if explicit else Path(DEFAULT_ENV_FILE)
    if not path.exists():
        if explicit:
            raise ConfigurationError(f"--env-file not found: {path}")
        return None
    loaded = 0
    for key, value in read_env_file(path).items():
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    logger.debug("Loaded %d variable(s) from %s", loaded, path)
    return path


def _build_config(args: argparse.Namespace) -> ParserConfig:
    args._env_file_used = _load_env_file(args)
    config = ParserConfig.load(getattr(args, "config", None))

    for override in getattr(args, "set", []) or []:
        key, _, value = override.partition("=")
        if not _:
            raise ConfigurationError(f"--set expects SECTION.KEY=VALUE, got {override!r}")
        config.set_path(key.strip(), value)

    if getattr(args, "target", None):
        config.graph.target = args.target
    if getattr(args, "output", None):
        config.export.output_dir = args.output
    if getattr(args, "log_level", None):
        config.runtime.log_level = args.log_level
    if getattr(args, "log_json", False):
        config.runtime.log_json = True

    if getattr(args, "format", None):
        config.export.formats = tuple(dict.fromkeys(args.format))
    if getattr(args, "state", None):
        config.state.path = args.state
    if getattr(args, "no_state", False):
        config.state.enabled = False
    if getattr(args, "full", False):
        config.state.mode = "full"
    if getattr(args, "ocr", False):
        config.ocr.enabled = True
    if getattr(args, "no_resources", False):
        # Turning off downloads must also turn off everything that consumes the
        # downloaded bytes, or validation rejects the combination as incoherent.
        config.images.download = False
        config.attachments.download = False
        config.attachments.extract_text = False
        config.ocr.enabled = False
    if getattr(args, "concurrency", None):
        config.runtime.concurrency = args.concurrency
    if getattr(args, "dry_run", False):
        config.runtime.dry_run = True

    filters = config.filters
    if getattr(args, "notebook", None):
        filters.notebook_names = tuple(args.notebook)
    if getattr(args, "notebook_id", None):
        filters.notebook_ids = tuple(args.notebook_id)
    if getattr(args, "section", None):
        filters.section_names = tuple(args.section)
    if getattr(args, "exclude_notebook", None):
        filters.exclude_notebook_names = tuple(args.exclude_notebook)
    if getattr(args, "exclude_section", None):
        filters.exclude_section_names = tuple(args.exclude_section)
    if getattr(args, "regex_filters", False):
        filters.use_regex = True
    if getattr(args, "modified_since", None):
        config.set_path("filters.modified_since", args.modified_since)
    if getattr(args, "max_pages", None):
        filters.max_pages = args.max_pages

    if getattr(args, "client_id", None):
        config.auth.client_id = args.client_id
    if getattr(args, "tenant_id", None):
        config.auth.tenant_id = args.tenant_id
    if getattr(args, "scope", None):
        config.auth.scopes = tuple(args.scope)
    return config


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_login(args: argparse.Namespace, config: ParserConfig) -> int:
    from .auth import DeviceCodeTokenProvider

    if not config.auth.client_id:
        raise ConfigurationError("login requires --client-id (or auth.client_id in the config)")
    cache = config.auth.token_cache_path
    provider = DeviceCodeTokenProvider(
        config.auth.client_id,
        tenant_id=config.auth.tenant_id,
        scopes=config.auth.scopes,
        authority=config.auth.authority,
        token_cache_path=cache,
    )
    token = provider.get_token()
    print(f"Signed in successfully. Token cache: {cache or '(disabled)'}")
    if cache:
        print("Later runs reuse this cache; no further sign-in is needed until it expires.")
    print(f"Access token length: {len(token)} characters")
    if args.print_refresh_token:
        refresh = provider.export_refresh_token()
        if refresh:
            print("\nRefresh token (store this in a secret manager, never in git):")
            print(refresh)
        else:
            print("\nNo refresh token in the cache; ensure 'offline_access' consent was granted.")
    return EXIT_OK


def cmd_discover(args: argparse.Namespace, config: ParserConfig) -> int:
    from .discovery import list_recent_notebooks
    from .pipeline import OneNoteParser

    if args.recent:
        with OneNoteParser(config) as parser:
            notebooks = list_recent_notebooks(parser.client, resolve_targets=True)
        if args.json:
            json.dump([n.to_dict() for n in notebooks], sys.stdout, indent=2)
            sys.stdout.write("\n")
            return EXIT_OK
        if not notebooks:
            print("No recent notebooks returned. Open a notebook in OneNote first.")
            return EXIT_OK
        print(f"{len(notebooks)} notebook(s) recently opened by this identity:\n")
        for notebook in notebooks:
            accessed = notebook.last_accessed.date().isoformat() if notebook.last_accessed else "-"
            print(f"  {notebook.display_name}")
            print(f"    last opened : {accessed}   source: {notebook.source_service or '-'}")
            print(f"    url         : {notebook.web_url or '-'}")
            target = notebook.resolved_target or "me"
            print(f"    target      : {target}")
            print(f"      onenote-parser discover --target '{target}' "
                  f"--notebook '{notebook.display_name}'\n")
        print("If a site shows target 'me' it could not be resolved - the identity may lack "
              "access, or the notebook is not SharePoint-hosted.")
        return EXIT_OK

    with OneNoteParser(config) as parser:
        tree = parser.discover()
    payload = tree.to_dict()
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
        print(f"Wrote hierarchy to {args.out}")
    if args.json:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        _print_tree(tree)
    return EXIT_OK


def _print_tree(tree: Any) -> None:
    counts = tree.counts()
    for notebook in tree.notebooks:
        print(f"[notebook] {notebook.display_name}")
        for group in notebook.section_groups:
            _print_group(group, depth=1)
        for section in notebook.sections:
            _print_section(section, depth=1)
    print(
        f"\n{counts['notebooks']} notebook(s), {counts['section_groups']} section group(s), "
        f"{counts['sections']} section(s), {counts['pages']} page(s)"
    )


def _print_group(group: Any, depth: int) -> None:
    print(f"{'  ' * depth}[group] {group.display_name}")
    for child in group.section_groups:
        _print_group(child, depth + 1)
    for section in group.sections:
        _print_section(section, depth + 1)


def _print_section(section: Any, depth: int) -> None:
    suffix = f"  !! {section.error}" if section.error else ""
    print(f"{'  ' * depth}[section] {section.display_name} ({len(section.pages)} pages){suffix}")
    for page in section.pages:
        print(f"{'  ' * (depth + 1 + page.level)}- {page.title or '(untitled)'}")


def cmd_run(args: argparse.Namespace, config: ParserConfig) -> int:
    from .pipeline import OneNoteParser

    with OneNoteParser(config) as parser:
        result = parser.run()

    counts = result.changes.counts()
    stats = result.stats
    print(f"\nRun {result.run_id} finished in {stats.duration_seconds:.1f}s")
    print(f"  pages: {stats.pages_parsed} parsed, {stats.pages_skipped} unchanged, "
          f"{stats.pages_failed} failed, {stats.pages_discovered} discovered")
    print("  changes: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"  resources: {stats.images_downloaded} image(s), "
          f"{stats.attachments_downloaded} attachment(s), {stats.images_ocr} OCR'd")
    print(f"  graph: {stats.graph_requests} request(s), {stats.graph_retries} retry/ies, "
          f"{stats.graph_throttled} throttled")
    if not config.runtime.dry_run and config.export.formats:
        print(f"  output: {config.output_path}")
    if result.errors:
        print(f"  errors: {len(result.errors)} (see manifest.json)")
        return EXIT_ERROR
    return EXIT_OK


def cmd_page(args: argparse.Namespace, config: ParserConfig) -> int:
    from .parsing.normalize import render_markdown
    from .pipeline import OneNoteParser

    if not args.page_id and not args.html_file:
        raise ConfigurationError("page requires a page id or --html-file")

    if args.html_file:
        # Offline path: no Graph client, no credentials needed.
        from .parsing import OneNoteHtmlParser

        html = Path(args.html_file).read_text(encoding="utf-8")
        parsed = OneNoteHtmlParser(config.content).parse(html, page_id=args.page_id)
        if args.markdown:
            output = render_markdown(parsed.blocks)
        else:
            output = json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": args.page_id or Path(args.html_file).stem,
                    "title": parsed.title,
                    "warnings": parsed.warnings,
                    "blocks": [b.to_dict() for b in parsed.blocks],
                },
                indent=2, ensure_ascii=False,
            )
    else:
        with OneNoteParser(config) as parser:
            page = parser.parse_page(
                PageRef(id=args.page_id), download_resources=not args.no_resources
            )
        output = render_markdown(page.blocks) if args.markdown else json.dumps(
            page.to_dict(), indent=2, ensure_ascii=False
        )

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(output + "\n", encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(output)
    return EXIT_OK


def cmd_status(args: argparse.Namespace, config: ParserConfig) -> int:
    path = args.state or config.state.path
    store = StateStore(path, config.state).load()
    by_status: dict[str, int] = {}
    for page_id in store.known_page_ids():
        state = store.get(page_id)
        if state:
            by_status[state.status] = by_status.get(state.status, 0) + 1
    payload = {
        "state_path": str(path),
        "exists": Path(path).exists(),
        "page_count": len(store),
        "previous_run_id": store.previous_run_id,
        "updated_at": store.loaded_at.isoformat() if store.loaded_at else None,
        "by_status": by_status,
    }
    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"State file : {payload['state_path']}"
              f"{'' if payload['exists'] else '  (not created yet)'}")
        print(f"Pages      : {payload['page_count']}")
        print(f"Last run   : {payload['previous_run_id'] or '-'} at {payload['updated_at'] or '-'}")
        for status in ChangeStatus:
            if status.value in by_status:
                print(f"  {status.value:14s}: {by_status[status.value]}")
    return EXIT_OK


def cmd_config(args: argparse.Namespace, config: ParserConfig) -> int:
    json.dump(config.to_dict(), sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return EXIT_OK


def cmd_schema(args: argparse.Namespace, _config: Optional[ParserConfig]) -> int:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "package_version": __version__,
        "block_types": [b.value for b in BlockType],
        "change_statuses": [s.value for s in ChangeStatus],
        "documents": {
            "index.json": "Notebook hierarchy, counts and the page catalogue",
            "manifest.json": "Run stats, change set and errors",
            "pages/<title>--<hash>.json": "One normalized page",
            "pages.jsonl": "One normalized page per line",
            "changes.jsonl": "One change record per line",
            "markdown/": "Browsable Markdown mirror of the hierarchy",
        },
    }
    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"Output schema version : {payload['schema_version']}")
        print(f"Package version       : {payload['package_version']}")
        print("Block types           : " + ", ".join(payload["block_types"]))
        print("Change statuses       : " + ", ".join(payload["change_statuses"]))
        print("\nExported documents:")
        for name, description in payload["documents"].items():
            print(f"  {name:34s} {description}")
    return EXIT_OK


COMMANDS = {
    "login": cmd_login,
    "discover": cmd_discover,
    "run": cmd_run,
    "page": cmd_page,
    "status": cmd_status,
    "config": cmd_config,
    "schema": cmd_schema,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "schema":
        return cmd_schema(args, None)

    try:
        config = _build_config(args)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    configure_logging(config.runtime.log_level, json_format=config.runtime.log_json)
    try:
        return COMMANDS[args.command](args, config)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except AuthenticationError as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        return EXIT_AUTH
    except OneNoteParserError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        logger.debug("Command failed", exc_info=True)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nInterrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
