# onenote-parser

Production-ready extraction and normalization of **Microsoft OneNote notebooks**
via Microsoft Graph.

It discovers a notebook hierarchy recursively, retrieves every accessible page,
parses OneNote HTML into ordered content blocks, downloads and processes images
and attachments, detects what changed since the last run, and exports a stable,
documented, JSON-serializable model.

The parser's responsibility **ends at export**. There is no embedding, vector
store, retrieval, ranking, LLM or chat surface here — by design. The output is
built to feed RAG pipelines, search indexes, migrations, analytics and
knowledge-management systems, none of which should have to understand OneNote
HTML.

---

## Highlights

- **Full hierarchy.** Notebooks → arbitrarily nested section groups → sections →
  pages → subpages, with ids, paths, timestamps and deep links at every level.
- **Loss-free normalization.** Headings, paragraphs, ordered/unordered lists,
  tables (with spans), images, attachments, math, code, quotes, dividers — and
  anything unrecognised preserved verbatim as an `unsupported` block.
- **Non-destructive rich text.** Inline formatting and links are stored as
  offset ranges over plain text, so `text` is chunk-ready while bold/italic/link
  information stays recoverable.
- **Internal links resolved.** `onenote:` deep links are classified and their
  `page-id` / `section-id` / `notebook-id` extracted — the edges of your
  internal link graph.
- **Resources handled.** Content-addressed, deduplicated image and attachment
  storage; text extraction from PDF, DOCX, PPTX, XLSX, HTML and plain text;
  optional pluggable OCR for screenshots, scans and handwriting.
- **Real change detection.** `new` / `modified` / `unchanged` / `deleted` /
  `inaccessible`, with a reason for each — and losing access is never reported
  as a deletion.
- **Built for OneNote's throttling.** Client-side token bucket, jittered
  backoff, and a `429`-without-`Retry-After` fallback, because OneNote does that.
- **Reusable.** Every stage is a documented interface: `TokenProvider`,
  `Transport`, `OcrEngine`, `TextExtractor`, `Exporter`, `StateStore`.

---

## Install

```bash
pip install -e .                      # core: requests + beautifulsoup4
pip install -e ".[auth]"              # + MSAL sign-in
pip install -e ".[ocr]"               # + pytesseract/Pillow (needs the tesseract binary)
pip install -e ".[pdf,xlsx,yaml]"     # + pypdf, openpyxl, PyYAML
pip install -e ".[all,dev]"           # everything + pytest/mypy/ruff
```

Python 3.10+.

---

## Quick start

### 1. Sign in once

Microsoft **retired app-only access to the OneNote API on 2025-03-31**, so the
parser runs under a delegated identity — typically a dedicated service account.

```bash
onenote-parser login --client-id <app-id> --tenant-id <tenant> --print-refresh-token
```

Store the printed refresh token in a secret manager and set:

```bash
cp .env.example .env      # then fill it in; the CLI auto-loads ./.env
```

```ini
ONENOTE_AUTH_MODE=refresh_token
ONENOTE_CLIENT_ID=…
ONENOTE_TENANT_ID=…
ONENOTE_CLIENT_SECRET=…          # the secret's *Value*, not its Secret ID
ONENOTE_REFRESH_TOKEN=…
ONENOTE_TARGET=users/onenote-svc@contoso.com
```

`./.env` is loaded automatically; real environment variables always win over it.
Use `--env-file PATH` for another location, or `--no-env-file` to ignore it.
Never put secrets in the YAML config — `.env` is gitignored, `config.yaml` is
too, but `.env.example` is not.

### 2. Look before you leap

```bash
onenote-parser discover --config config.yaml            # hierarchy only, no content
onenote-parser run --config config.yaml --dry-run       # + change report, still no content
```

### 3. Run it

```bash
onenote-parser run --config config.yaml --format json --format markdown
```

```
Run 4c1f9a2b8e7d0a13 finished in 41.2s
  pages: 12 parsed, 124 unchanged, 0 failed, 137 discovered
  changes: new=4, modified=8, unchanged=124, deleted=1, inaccessible=0
  resources: 30 image(s), 3 attachment(s), 0 OCR'd
  graph: 212 request(s), 3 retry/ies, 2 throttled
  output: output
```

Re-run it tomorrow and only what actually changed is fetched and re-exported.

---

## Library use

```python
from onenote_parser import OneNoteParser, ParserConfig

config = ParserConfig.load("config.yaml")
config.filters.notebook_names = ("Engineering",)

with OneNoteParser(config) as parser:
    result = parser.run()

print(result.changes.counts())
for page in result.pages:
    print(page.breadcrumb, "-", len(page.blocks), "blocks")
```

Drive one stage at a time:

```python
with OneNoteParser(config) as parser:
    tree = parser.discover()                      # hierarchy, no page content
    page = parser.parse_page("1-abc123!456")      # one page, fully enriched
    parser.export(result)                         # write an existing result
```

Parse archived HTML with no credentials and no network:

```python
from onenote_parser import OneNoteHtmlParser, render_markdown

result = OneNoteHtmlParser().parse(open("page.html").read())
print(render_markdown(result.blocks))
```

Bring your own token, HTTP stack, OCR engine or exporter:

```python
from onenote_parser.auth import CallableTokenProvider
from onenote_parser.resources import CallableOcrEngine, register_extractor

parser = OneNoteParser(
    config,
    token_provider=CallableTokenProvider(lambda: my_identity.get_token()),
    ocr_engine=CallableOcrEngine(lambda png: my_vision_model.caption(png), name="vision"),
)
```

See [`examples/`](examples/) for runnable scripts.

---

## Output

```
output/
├── index.json                       hierarchy, counts, page catalogue
├── manifest.json                    run stats, change set, errors
├── pages/<title>--<hash>.json       one normalized page per file
├── pages.jsonl                      one page per line, for stream ingestion
├── changes.jsonl                    one change record per line
├── markdown/                        browsable mirror of the hierarchy
└── resources/{images,attachments}/  content-addressed, deduplicated
```

An **incremental run exports a delta**: unchanged pages are not re-fetched, so
they are not re-exported. `index.json` labels this explicitly
(`"export_scope": "changed_pages"`), its `tree` is always the complete
hierarchy, and artifacts for pages reported `deleted` are pruned from the output
so stale content never lingers. Use `--full` for a complete export.

A page, abbreviated:

```jsonc
{
  "schema_version": "1.0",
  "id": "1-a1b2c3!456",
  "title": "Pump Commissioning",
  "breadcrumb": "Engineering > Pumps > Centrifugal > Commissioning > Pump Commissioning",
  "path": ["Engineering", "Pumps", "Centrifugal", "Commissioning"],
  "metadata": {"modified_at": "2024-06-10T09:30:00Z", "level": 0,
               "web_url": "https://onenote.com/…"},
  "flags": {"has_tables": true, "has_images": true, "has_onenote_links": true},
  "content_hash": "8f14e45fceea167a…",
  "text": "Scope and Purpose\n\nThis procedure covers centrifugal pumps…",
  "blocks": [
    {"type": "heading", "order": 0, "level": 2, "source_id": "h2-scope",
     "content": {"text": "Scope and Purpose"}},
    {"type": "paragraph", "order": 1, "content": {
       "text": "This procedure covers centrifugal pumps, per ISO 9906.",
       "marks": [{"start": 22, "end": 39, "type": "bold"},
                 {"start": 45, "end": 53, "type": "link",
                  "attrs": {"href": "https://…", "kind": "external"}}]}}
  ]
}
```

The complete contract — every block type, the change records, the compatibility
policy — is in **[`docs/output-schema.md`](docs/output-schema.md)**.

---

## Configuration

Layered, lowest to highest: **defaults → config file → environment → CLI flags**.

```bash
onenote-parser run \
  --config config.yaml \
  --target users/onenote-svc@contoso.com \
  --notebook Engineering --exclude-section Scratch \
  --format json --format jsonl \
  --set graph.requests_per_minute=120
```

Any setting is reachable from the environment as
`ONENOTE_PARSER__<section>__<key>`, plus friendly aliases
(`ONENOTE_ACCESS_TOKEN`, `ONENOTE_TARGET`, `ONENOTE_OUTPUT_DIR`, …). See
[`config.example.yaml`](config.example.yaml) and [`.env.example`](.env.example)
for every option with its default. `ParserConfig.validate()` fails fast with an
actionable message instead of breaking mid-crawl, and returns non-fatal
warnings (for example, a `requests_per_minute` above OneNote's documented
ceiling).

---

## CLI

| Command | Purpose |
|---|---|
| `login` | Device-code sign-in; caches tokens, can print a refresh token |
| `discover` | Print the hierarchy without fetching page content |
| `run` | The full pipeline: discover, parse, enrich, export |
| `page` | Parse one page by id, or a local HTML file offline |
| `status` | Summarize the incremental-sync state file |
| `config` | Print the effective, secret-redacted configuration |
| `schema` | Print the output schema version and block catalogue |

```bash
onenote-parser page --html-file cached.html --markdown   # no credentials needed
onenote-parser status --state state/onenote-state.json
onenote-parser run --full --no-state                     # force a complete re-parse
```

Exit codes: `0` success · `1` completed with errors · `2` configuration ·
`3` authentication.

---

## Tests

```bash
python -m pytest -q                                        # 274 tests, ~2s, no network
python -m pytest --cov=onenote_parser --cov-report=term-missing
```

The suite runs entirely offline against an in-memory Graph stand-in that serves
a notebook with nested section groups, subpages, a permission-denied section,
binary resources and a deleted page — so throttling, retries, incremental sync,
deletion detection and concurrency determinism are all exercised for real.

---

## Documentation

- **[docs/onenote-parser.md](docs/onenote-parser.md)** — architecture, auth
  setup, throttling strategy, change detection, known limitations, operations.
- **[docs/output-schema.md](docs/output-schema.md)** — the output contract.

## License

MIT
