"""Unit tests for configuration parsing and URL normalisation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from csmt.config import Settings, load_env_file, normalize_base_url
from csmt.errors import ConfigError


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://host", ("https://host", "/0/odata")),
        ("https://host/", ("https://host", "/0/odata")),
        ("https://host/0/odata", ("https://host", "/0/odata")),
        ("https://host/Creatio/0/odata", ("https://host/Creatio", "/0/odata")),
        ("host.example.com", ("https://host.example.com", "/0/odata")),
        ("https://host/odata", ("https://host", "/odata")),
    ],
)
def test_normalize_base_url(raw: str, expected: tuple[str, str]) -> None:
    assert normalize_base_url(raw) == expected


def test_normalize_base_url_custom_path() -> None:
    assert normalize_base_url("https://host", "/odata") == ("https://host", "/odata")
    assert normalize_base_url("https://host/0/odata", "/custom") == ("https://host", "/0/odata")


def test_normalize_base_url_rejects_empty() -> None:
    with pytest.raises(ConfigError):
        normalize_base_url("   ")


def test_settings_from_env() -> None:
    env = {
        "CREATIO_BASE_URL": "https://mycreatio.example/0/odata",
        "CREATIO_USERNAME": "Supervisor",
        "CREATIO_PASSWORD": "p@ss",
        "CREATIO_ENTITY": "Contact",
        "CREATIO_VERIFY_SSL": "false",
        "CREATIO_TIMEOUT": "12.5",
        "CREATIO_MAX_RETRIES": "7",
        "CREATIO_PAGE_SIZE": "250",
    }
    settings = Settings.from_env(env=env)
    assert settings.odata_url == "https://mycreatio.example/0/odata"
    assert settings.verify_ssl is False
    assert settings.timeout == 12.5
    assert settings.max_retries == 7
    assert settings.page_size == 250
    assert settings.resolved_auth_mode == "forms"


def test_settings_requires_url() -> None:
    with pytest.raises(ConfigError):
        Settings(base_url="")


def test_auto_auth_mode_prefers_oauth() -> None:
    settings = Settings(
        base_url="https://host",
        client_id="id",
        client_secret="secret",
        identity_url="https://host-is",
        username="u",
        password="p",
    )
    assert settings.resolved_auth_mode == "oauth2"


def test_oauth2_requires_identity_url() -> None:
    settings = Settings(base_url="https://host", client_id="id", client_secret="secret", auth_mode="oauth2")
    with pytest.raises(ConfigError) as excinfo:
        _ = settings.resolved_auth_mode
    assert "OAuth20IdentityServerUrl" in str(excinfo.value)
    with pytest.raises(ConfigError):
        _ = settings.token_endpoint


@pytest.mark.parametrize(
    "identity,expected",
    [
        ("https://host-is.example.com", "https://host-is.example.com/connect/token"),
        ("https://host-is.example.com/", "https://host-is.example.com/connect/token"),
        ("https://host-is.example.com/connect/token", "https://host-is.example.com/connect/token"),
    ],
)
def test_token_endpoint_from_identity_url(identity: str, expected: str) -> None:
    settings = Settings(
        base_url="https://host", client_id="id", client_secret="s",
        auth_mode="oauth2", identity_url=identity,
    )
    assert settings.token_endpoint == expected


def test_token_endpoint_rejects_relative_identity() -> None:
    settings = Settings(
        base_url="https://host", client_id="id", client_secret="s",
        auth_mode="oauth2", identity_url="/is",
    )
    with pytest.raises(ConfigError):
        _ = settings.token_endpoint


def test_identity_url_from_env() -> None:
    settings = Settings.from_env(env={
        "CREATIO_BASE_URL": "https://host",
        "CREATIO_AUTH_MODE": "oauth2",
        "CREATIO_CLIENT_ID": "id",
        "CREATIO_CLIENT_SECRET": "s",
        "CREATIO_IDENTITY_URL": "https://host-is",
    })
    assert settings.token_endpoint == "https://host-is/connect/token"


def test_auto_auth_mode_without_credentials() -> None:
    settings = Settings(base_url="https://host")
    with pytest.raises(ConfigError):
        _ = settings.resolved_auth_mode


def test_explicit_auth_mode_requires_matching_credentials() -> None:
    settings = Settings(base_url="https://host", auth_mode="oauth2")
    with pytest.raises(ConfigError):
        _ = settings.resolved_auth_mode


def test_env_file_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CREATIO_BASE_URL", raising=False)
    monkeypatch.delenv("CUSTOM_QUOTED", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# comment\n"
        "export CREATIO_BASE_URL=https://host\n"
        'CUSTOM_QUOTED="hello world"\n'
        "IGNORED_LINE\n",
        encoding="utf-8",
    )
    loaded = load_env_file(env_path)
    assert loaded["CREATIO_BASE_URL"] == "https://host"
    assert os.environ["CUSTOM_QUOTED"] == "hello world"

    monkeypatch.setenv("CREATIO_BASE_URL", "https://from-environment")
    load_env_file(env_path)
    assert os.environ["CREATIO_BASE_URL"] == "https://from-environment"
    load_env_file(env_path, override=True)
    assert os.environ["CREATIO_BASE_URL"] == "https://host"


def test_env_file_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_env_file(tmp_path / "nope.env")


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://pegadaian.creatio.com", "https://pegadaian-is.creatio.com"),
        ("pegadaian.creatio.com/0/odata", "https://pegadaian-is.creatio.com"),
        ("https://Pegadaian.Creatio.com", "https://pegadaian-is.creatio.com"),
        ("https://host", None),  # custom domain: no default
        ("https://a.b.creatio.com", None),  # nested subdomain: not a cloud site
        ("https://127.0.0.1:5000", None),
    ],
)
def test_default_identity_url(base: str, expected: str | None) -> None:
    from csmt.config import default_identity_url

    assert default_identity_url(base) == expected


def test_cloud_site_uses_default_identity_when_not_set() -> None:
    settings = Settings(
        base_url="https://pegadaian.creatio.com", client_id="id", client_secret="s", auth_mode="oauth2"
    )
    assert settings.resolved_identity_url == "https://pegadaian-is.creatio.com"
    assert settings.token_endpoint == "https://pegadaian-is.creatio.com/connect/token"
    assert settings.resolved_auth_mode == "oauth2"


def test_explicit_identity_overrides_default() -> None:
    settings = Settings(
        base_url="https://pegadaian.creatio.com", client_id="id", client_secret="s",
        auth_mode="oauth2", identity_url="https://custom-is.example.com",
    )
    assert settings.token_endpoint == "https://custom-is.example.com/connect/token"


def test_identity_candidates_prefer_hyphen_then_underscore() -> None:
    """Real tenant: hyphen works (400 invalid_client), underscore is a dead host."""
    from csmt.config import identity_url_candidates

    assert identity_url_candidates("https://pgdn-demo.creatio.com") == [
        "https://pgdn-demo-is.creatio.com",
        "https://pgdn-demo_is.creatio.com",
    ]


def test_explicit_identity_is_tried_first_with_derived_fallbacks() -> None:
    """A stale implicit-style value must not block the run when the real host exists."""
    settings = Settings(
        base_url="https://pgdn-demo.creatio.com", client_id="a", client_secret="b",
        auth_mode="oauth2", identity_url="https://pgdn-demo_is.creatio.com/",
    )
    assert settings.identity_candidates == [
        "https://pgdn-demo_is.creatio.com",
        "https://pgdn-demo-is.creatio.com",
    ]


def test_explicit_custom_identity_has_no_derived_fallbacks() -> None:
    """A clearly custom host (not a creatio.com tenant) is used alone."""
    settings = Settings(
        base_url="https://host.local", client_id="a", client_secret="b",
        auth_mode="oauth2", identity_url="https://idp.internal/",
    )
    assert settings.identity_candidates == ["https://idp.internal"]
