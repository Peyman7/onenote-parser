"""CLI argument handling and the offline commands."""

from __future__ import annotations

import json

import pytest

from onenote_parser import cli


def run(argv, capsys):
    code = cli.main(argv)
    return code, capsys.readouterr()


def test_schema_command_lists_block_types(capsys):
    code, out = run(["schema"], capsys)
    assert code == cli.EXIT_OK
    assert "heading" in out.out and "Output schema version" in out.out


def test_schema_json(capsys):
    code, out = run(["schema", "--json"], capsys)
    payload = json.loads(out.out)
    assert code == cli.EXIT_OK
    assert payload["schema_version"] and "unsupported" in payload["block_types"]


def test_page_command_parses_a_local_html_file(capsys, tmp_path):
    html = tmp_path / "page.html"
    html.write_text("<html><head><title>Offline</title></head><body><p>hi</p></body></html>")
    code, out = run(["page", "--html-file", str(html)], capsys)
    payload = json.loads(out.out)
    assert code == cli.EXIT_OK
    assert payload["title"] == "Offline"
    assert payload["blocks"][0]["content"]["text"] == "hi"


def test_page_command_markdown_output(capsys, tmp_path, rich_html):
    html = tmp_path / "page.html"
    html.write_text(rich_html)
    code, out = run(["page", "--html-file", str(html), "--markdown"], capsys)
    assert code == cli.EXIT_OK
    assert "## Scope and Purpose" in out.out


def test_page_command_writes_to_a_file(capsys, tmp_path):
    html = tmp_path / "page.html"
    html.write_text("<html><body><p>hi</p></body></html>")
    target = tmp_path / "nested" / "out.json"
    code, out = run(["page", "--html-file", str(html), "--out", str(target)], capsys)
    assert code == cli.EXIT_OK and target.exists()
    assert json.loads(target.read_text())["blocks"][0]["content"]["text"] == "hi"


def test_page_command_without_id_or_file_is_a_config_error(capsys):
    code, out = run(["page"], capsys)
    assert code == cli.EXIT_CONFIG
    assert "page id or --html-file" in out.err


def test_config_command_redacts_secrets(capsys, tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"auth": {"mode": "static", "access_token": "hunter2"}}))
    code, out = run(["config", "--config", str(path)], capsys)
    payload = json.loads(out.out)
    assert code == cli.EXIT_OK
    assert payload["auth"]["access_token"] == "***redacted***"


def test_status_command_on_a_missing_state_file(capsys, tmp_path):
    code, out = run(["status", "--state", str(tmp_path / "none.json")], capsys)
    assert code == cli.EXIT_OK
    assert "not created yet" in out.out


def test_status_command_summarizes_an_existing_state(capsys, tmp_path):
    from onenote_parser.models import PageMetadata, ParsedPage
    from onenote_parser.state import StateStore

    path = tmp_path / "state.json"
    store = StateStore(path)
    store.record_parsed(ParsedPage(id="pg-1", title="A", content_hash="h",
                                   metadata=PageMetadata()))
    store.save(run_id="run-1")

    code, out = run(["status", "--state", str(path), "--json"], capsys)
    payload = json.loads(out.out)
    assert code == cli.EXIT_OK
    assert payload["page_count"] == 1 and payload["previous_run_id"] == "run-1"


def test_missing_config_file_reports_a_config_error(capsys):
    code, out = run(["config", "--config", "/nonexistent/config.yaml"], capsys)
    assert code == cli.EXIT_CONFIG and "not found" in out.err


def test_bad_set_override_is_rejected(capsys):
    code, out = run(["config", "--set", "graph.timeout_seconds"], capsys)
    assert code == cli.EXIT_CONFIG


def test_unknown_set_key_is_rejected(capsys):
    code, out = run(["config", "--set", "graph.nonexistent=1"], capsys)
    assert code == cli.EXIT_CONFIG and "Unknown configuration key" in out.err


# -- flag plumbing ----------------------------------------------------------- #


def make_args(argv):
    return cli.build_parser().parse_args(argv)


def test_cli_flags_map_onto_the_config():
    config = cli._build_config(make_args([
        "run", "--target", "users/svc@x.com", "--output", "/tmp/out",
        "--format", "jsonl", "--format", "markdown",
        "--notebook", "Engineering", "--exclude-section", "Scratch",
        "--max-pages", "10", "--concurrency", "8", "--ocr",
        "--modified-since", "2024-06-01T00:00:00Z",
        "--set", "graph.requests_per_minute=90",
    ]))
    assert config.graph.target == "users/svc@x.com"
    assert config.export.output_dir == "/tmp/out"
    assert config.export.formats == ("jsonl", "markdown")
    assert config.filters.notebook_names == ("Engineering",)
    assert config.filters.exclude_section_names == ("Scratch",)
    assert config.filters.max_pages == 10
    assert config.runtime.concurrency == 8
    assert config.ocr.enabled is True
    assert config.filters.modified_since.year == 2024
    assert config.graph.requests_per_minute == 90


def test_no_resources_produces_a_coherent_config():
    """Disabling downloads must also disable everything that consumes them."""
    config = cli._build_config(make_args(["run", "--no-resources"]))
    config.auth.mode = "static"
    config.auth.access_token = "t"
    assert config.images.download is False
    assert config.attachments.extract_text is False
    assert config.validate() == []


def test_full_and_no_state_flags():
    assert cli._build_config(make_args(["run", "--full"])).state.mode == "full"
    assert cli._build_config(make_args(["run", "--no-state"])).state.enabled is False


def test_dry_run_flag():
    assert cli._build_config(make_args(["run", "--dry-run"])).runtime.dry_run is True


def test_login_flags_populate_auth():
    config = cli._build_config(make_args([
        "login", "--client-id", "app", "--tenant-id", "tenant", "--scope", "Notes.Read.All",
    ]))
    assert config.auth.client_id == "app" and config.auth.tenant_id == "tenant"
    assert config.auth.scopes == ("Notes.Read.All",)


def test_version_flag_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0


# -- .env loading ------------------------------------------------------------ #


def test_env_file_is_auto_loaded_from_the_working_directory(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(
        "# comment\n"
        "ONENOTE_CLIENT_ID=from-env-file\n"
        "export ONENOTE_TENANT_ID='quoted-tenant'\n"
        "\n"
        "ONENOTE_PARSER__runtime__concurrency=7\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ONENOTE_CLIENT_ID", raising=False)
    monkeypatch.delenv("ONENOTE_TENANT_ID", raising=False)

    config = cli._build_config(cli.build_parser().parse_args(["config"]))
    assert config.auth.client_id == "from-env-file"
    assert config.auth.tenant_id == "quoted-tenant"
    assert config.runtime.concurrency == 7


def test_real_environment_wins_over_the_env_file(tmp_path, monkeypatch):
    """A shell export or an orchestrator-injected value must not be overridden."""
    (tmp_path / ".env").write_text("ONENOTE_CLIENT_ID=from-file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONENOTE_CLIENT_ID", "from-shell")

    config = cli._build_config(cli.build_parser().parse_args(["config"]))
    assert config.auth.client_id == "from-shell"


def test_explicit_env_file_flag(tmp_path, monkeypatch):
    target = tmp_path / "secrets.env"
    target.write_text("ONENOTE_CLIENT_ID=explicit\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ONENOTE_CLIENT_ID", raising=False)

    config = cli._build_config(
        cli.build_parser().parse_args(["config", "--env-file", str(target)])
    )
    assert config.auth.client_id == "explicit"


def test_missing_explicit_env_file_is_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code, out = run(["config", "--env-file", str(tmp_path / "nope.env")], capsys)
    assert code == cli.EXIT_CONFIG and "not found" in out.err


def test_no_env_file_flag_skips_the_default(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ONENOTE_CLIENT_ID=from-file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ONENOTE_CLIENT_ID", raising=False)

    config = cli._build_config(cli.build_parser().parse_args(["config", "--no-env-file"]))
    assert config.auth.client_id is None


def test_missing_default_env_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli._build_config(cli.build_parser().parse_args(["config"])) is not None


def test_env_file_parsing_tolerates_junk_and_preserves_literal_dollars(tmp_path):
    from onenote_parser.config import read_env_file

    path = tmp_path / ".env"
    path.write_text(
        "GOOD=value\n"
        "malformed line without equals\n"
        "  # indented comment\n"
        'QUOTED="has spaces"\n'
        "SECRET=Xy8Q~with$dollar$signs\n"
        "EMPTY=\n"
    )
    values = read_env_file(path)
    assert values == {
        "GOOD": "value",
        "QUOTED": "has spaces",
        "SECRET": "Xy8Q~with$dollar$signs",   # never interpolated
        "EMPTY": "",
    }
