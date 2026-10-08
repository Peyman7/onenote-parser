"""Configuration layering, coercion and validation."""

from __future__ import annotations

import json
from datetime import timezone

import pytest

from onenote_parser.config import ParserConfig
from onenote_parser.errors import ConfigurationError


def test_defaults_are_valid_for_device_code_with_client_id():
    config = ParserConfig()
    config.auth.client_id = "app-id"
    assert config.validate() == []


def test_env_aliases_and_generic_overrides():
    config = ParserConfig()
    config.apply_env(
        {
            "ONENOTE_ACCESS_TOKEN": "tok",
            "ONENOTE_TARGET": "users/svc@contoso.com",
            "ONENOTE_PARSER__graph__requests_per_minute": "120",
            "ONENOTE_PARSER__ocr__languages": "eng,deu",
            "ONENOTE_PARSER__images__download": "false",
            "UNRELATED": "ignored",
        }
    )
    assert config.auth.access_token == "tok"
    assert config.graph.target == "users/svc@contoso.com"
    assert config.graph.requests_per_minute == 120
    assert config.ocr.languages == ("eng", "deu")
    assert config.images.download is False


def test_from_dict_rejects_unknown_keys():
    with pytest.raises(ConfigurationError) as excinfo:
        ParserConfig.from_dict({"graph": {"nope": 1}})
    assert "graph.nope" in str(excinfo.value)


def test_from_file_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"export": {"formats": ["json", "markdown"], "pretty": False}}))
    config = ParserConfig.from_file(path)
    assert config.export.formats == ("json", "markdown")
    assert config.export.pretty is False


def test_modified_since_is_parsed_into_aware_datetime():
    config = ParserConfig()
    config.set_path("filters.modified_since", "2024-06-01T00:00:00Z")
    assert config.filters.modified_since.tzinfo is not None
    assert config.filters.modified_since.astimezone(timezone.utc).year == 2024


def test_validate_reports_all_problems_at_once():
    config = ParserConfig()
    config.auth.mode = "refresh_token"
    config.graph.page_size = 500
    config.export.formats = ("parquet",)
    with pytest.raises(ConfigurationError) as excinfo:
        config.validate()
    message = str(excinfo.value)
    assert "client_id" in message and "page_size" in message and "parquet" in message


def test_client_credentials_warns_about_retired_onenote_app_only_auth():
    config = ParserConfig()
    config.auth.mode = "client_credentials"
    config.auth.client_id = "app"
    config.auth.client_secret = "secret"
    config.auth.tenant_id = "tenant-guid"
    warnings = config.validate()
    assert any("2025-03-31" in w for w in warnings)


def test_ocr_requires_image_download():
    config = ParserConfig()
    config.auth.mode = "static"
    config.auth.access_token = "t"
    config.ocr.enabled = True
    config.images.download = False
    with pytest.raises(ConfigurationError):
        config.validate()


def test_to_dict_redacts_secrets():
    config = ParserConfig()
    config.auth.client_secret = "super-secret"
    config.auth.refresh_token = "rt"
    payload = config.to_dict()
    assert payload["auth"]["client_secret"] == "***redacted***"
    assert payload["auth"]["refresh_token"] == "***redacted***"
    assert config.to_dict(redact_secrets=False)["auth"]["client_secret"] == "super-secret"


def test_resolve_output_keeps_absolute_paths(tmp_path):
    config = ParserConfig()
    config.export.output_dir = str(tmp_path / "out")
    assert config.resolve_output("resources/images") == tmp_path / "out" / "resources" / "images"
    assert config.resolve_output(str(tmp_path / "abs")) == tmp_path / "abs"


def test_token_cache_defaults_are_shared_between_login_and_later_runs(tmp_path):
    """`login` and `run` must agree on the cache path or every run re-prompts."""
    from onenote_parser.cli import _build_config, build_parser
    from onenote_parser.config import DEFAULT_TOKEN_CACHE_PATH

    login = _build_config(build_parser().parse_args(["login", "--client-id", "app"]))
    run = _build_config(build_parser().parse_args(["run"]))
    assert login.auth.token_cache_path == run.auth.token_cache_path == DEFAULT_TOKEN_CACHE_PATH


def test_token_cache_can_be_disabled_or_relocated():
    config = ParserConfig()
    config.apply_env({"ONENOTE_TOKEN_CACHE": "/secure/cache.json"})
    assert config.auth.token_cache_path == "/secure/cache.json"
    config.set_path("auth.token_cache_path", None)
    assert config.auth.token_cache_path is None
