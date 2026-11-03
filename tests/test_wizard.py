"""Wizard tests: prompts, env file generation, connection test, overwrite handling."""

from __future__ import annotations

import builtins
import getpass as getpass_module
import os
import stat
from pathlib import Path

import pytest
from conftest import make_settings

from csmt.cli import main
from csmt.config import load_env_file
from csmt.errors import ConfigError, CreatioError


class FakeInput:
    """Queue-driven replacement for builtins.input/getpass."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)

    def __call__(self, prompt: str = "") -> str:
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


@pytest.fixture(autouse=True)
def _isolate_environ():
    """The wizard exports .env values into os.environ; never leak them into other tests."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def _no_network_probe(monkeypatch: pytest.MonkeyPatch):
    """Wizard probes the identity endpoint and the OData path; keep tests offline."""
    monkeypatch.setattr(
        "csmt.cli._probe_identity_endpoint",
        lambda identity_url, **kwargs: ("token", "token diterima (offline test)"),
    )
    monkeypatch.setattr(
        "csmt.cli._probe_odata_path",
        lambda *args, **kwargs: ("ok", "path OData menjawab (offline test)"),
    )


def install_inputs(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    fake = FakeInput(answers)
    monkeypatch.setattr(builtins, "input", fake)
    monkeypatch.setattr(getpass_module, "getpass", fake)


def wizard_answers(**overrides) -> list[str]:
    default = {
        "url": "https://host",
        "runtime": "1",  # /odata (.NET 8)
        "auth": "2",  # forms
        "username": "Supervisor",
        "password": "secret",
        "entity": "Contact",
        "tls": "1",
        "test": "2",  # skip connection test by default
        "out": ".env",
        "overwrite": "1",
    }
    default.update(overrides)
    # The overwrite answer is only consumed when the target file exists.
    return [
        default["url"],
        default["runtime"],
        default["auth"],
        default["username"],
        default["password"],
        default["entity"],
        default["tls"],
        default["test"],
        default["overwrite"],
    ]


def test_wizard_generates_dotenv_forms(mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "wizard.env"
    install_inputs(
        monkeypatch,
        wizard_answers(out=str(out), entity="Contact", test="2"),
    )
    out.touch()  # force overwrite branch
    code = main(["wizard", "--out", str(out)])
    assert code == 0

    loaded = load_env_file(out, override=True)
    assert loaded["CREATIO_BASE_URL"] == "https://host"
    assert loaded["CREATIO_ODATA_PATH"] == "/0/odata"  # runtime default = .NET Framework
    assert loaded["CREATIO_AUTH_MODE"] == "forms"
    assert loaded["CREATIO_USERNAME"] == "Supervisor"
    assert loaded["CREATIO_PASSWORD"] == "secret"
    assert loaded["CREATIO_ENTITY"] == "Contact"
    assert loaded["CREATIO_VERIFY_SSL"] == "false"  # TLS verification defaults to off

    # credentials should be readable only by the owner
    assert stat.S_IMODE(out.stat().st_mode) & 0o077 == 0


def test_wizard_oauth2_branch(mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "oauth.env"
    install_inputs(
        monkeypatch,
        [
            "https://host/0/odata",  # paste full URL; must be normalised
            "2",  # runtime: .NET 8 (/odata)
            "1",  # oauth2 (default)
            "client",
            "client-secret",
            "",  # identity URL empty -> rejected, asked again
            "https://host-is.example.com",  # OAuth20IdentityServerUrl
            "",  # entity skipped
            "1",  # TLS verify yes
            "2",  # no connection test
        ],
    )
    code = main(["wizard", "--out", str(out)])
    assert code == 0
    loaded = load_env_file(out, override=True)
    assert loaded["CREATIO_BASE_URL"] == "https://host"
    assert loaded["CREATIO_ODATA_PATH"] == "/odata"  # option 2 = .NET 8
    assert loaded["CREATIO_AUTH_MODE"] == "oauth2"
    assert loaded["CREATIO_CLIENT_ID"] == "client"
    assert loaded["CREATIO_IDENTITY_URL"] == "https://host-is.example.com"
    assert "CREATIO_TOKEN_URL" not in loaded


def test_wizard_test_connection_success(mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    out = tmp_path / "env.test"
    mock.seed("Contact", {"Name": "U", "Email": "u@x"})
    install_inputs(
        monkeypatch,
        wizard_answers(
            url=mock.url,
            out=str(out),
            test="1",  # run the connection test
        ),
    )
    code = main(["wizard", "--out", str(out)])
    assert code == 0
    output = capsys.readouterr().out
    assert "entity_reachable=True" in output
    # check the written file, not os.environ (the wizard must not mutate the shell)
    assert load_env_file(out, override=True)["CREATIO_PASSWORD"] == "secret"


def test_wizard_test_connection_failure_still_saves(mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    out = tmp_path / "env.fail"
    install_inputs(
        monkeypatch,
        wizard_answers(
            url=mock.url,
            out=str(out),
            password="wrong",  # login rejected
            test="1",
        ),
    )
    code = main(["wizard", "--out", str(out)])
    assert code == 0  # wizard still saves the config
    assert "TIDAK BERHASIL" in capsys.readouterr().out
    assert load_env_file(out, override=True)["CREATIO_PASSWORD"] == "wrong"


def test_wizard_overwrite_makes_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / ".env"
    out.write_text("CREATIO_BASE_URL=https://old\n", encoding="utf-8")
    install_inputs(
        monkeypatch,
        wizard_answers(out=str(out), test="2", overwrite="1"),
    )
    code = main(["wizard", "--out", str(out)])
    assert code == 0
    backup = out.with_suffix(out.suffix + ".bak")
    assert backup.exists()
    assert "old" in backup.read_text(encoding="utf-8")
    assert "https://host" in out.read_text(encoding="utf-8")


def test_wizard_cancelled_on_overwrite_decline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / ".env"
    out.write_text("CREATIO_BASE_URL=https://old\n", encoding="utf-8")
    install_inputs(
        monkeypatch,
        wizard_answers(out=str(out), test="2", overwrite="2"),
    )
    code = main(["wizard", "--out", str(out)])
    assert code == 2
    assert out.read_text(encoding="utf-8") == "CREATIO_BASE_URL=https://old\n"


def test_wizard_runtime_has_only_two_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Option 3 (custom path) is gone; invalid input is rejected and re-asked."""
    out = tmp_path / "two.env"
    install_inputs(
        monkeypatch,
        ["https://host", "3", "2", "2", "1", "Supervisor", "secret", "", "1", "2"],
    )
    assert main(["wizard", "--out", str(out)]) == 0
    assert "Masukkan angka 1-2" in capsys.readouterr().out
    assert load_env_file(out, override=True)["CREATIO_ODATA_PATH"] == "/odata"


def test_wizard_empty_urls_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_inputs(
        monkeypatch,
        ["", "not a url", "https://host", "1", "2", "Supervisor", "secret", "", "1", "2"],
    )
    code = main(["wizard", "--out", str(tmp_path / "z.env")])
    # first two answers rejected; third becomes the URL; full flow continues
    assert code == 0
    assert load_env_file(tmp_path / "z.env", override=True)["CREATIO_BASE_URL"] == "https://host"


def test_wizard_abort_via_eof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_inputs(monkeypatch, ["", ""])  # nothing useful then EOF
    assert main(["wizard", "--out", str(tmp_path / "x.env")]) == 2


def test_wizard_settings_are_usable_for_import(mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The generated .env must drive a real import end to end."""
    out = tmp_path / ".env"
    mock.seed("Contact", {"Name": "Existing", "Email": "alice@example.com"})
    install_inputs(monkeypatch, wizard_answers(url=mock.url, out=str(out), test="1"))
    assert main(["wizard", "--out", str(out)]) == 0

    settings = make_settings(mock)
    monkeypatch.setenv("CREATIO_BASE_URL", mock.url)
    monkeypatch.setenv("CREATIO_USERNAME", "Supervisor")
    monkeypatch.setenv("CREATIO_PASSWORD", "secret")
    monkeypatch.setenv("CREATIO_ENTITY", "Contact")
    load_env_file(out, override=True)

    csv_path = tmp_path / "data.csv"
    csv_path.write_text("Name,Email\nAlice,alice@example.com\n", encoding="utf-8")
    report = tmp_path / "report.csv"
    code = main(
        [
            "import",
            "--csv",
            str(csv_path),
            "--env-file",
            str(out),
            "--mode",
            "upsert",
            "--key",
            "Email",
            "--report",
            str(report),
            "--no-journal",
            "--quiet",
        ]
    )
    assert code == 0
    assert len(mock.records("Contact")) == 1


def test_wizard_oauth_identity_default_for_cloud_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Enter at the identity prompt accepts the derived <subdomain>_is.creatio.com default."""
    out = tmp_path / "cloud.env"
    install_inputs(
        monkeypatch,
        [
            "https://pegadaian.creatio.com",
            "2",  # .NET Framework path (any runtime works for this check)
            "1",  # oauth2
            "client",
            "client-secret",
            "",  # identity prompt: Enter -> default
            "",  # entity skipped
            "1",  # TLS verify
            "2",  # no connection test
        ],
    )
    assert main(["wizard", "--out", str(out)]) == 0
    assert "https://pegadaian-is.creatio.com" in capsys.readouterr().out
    loaded = load_env_file(out, override=True)
    assert loaded["CREATIO_IDENTITY_URL"] == "https://pegadaian-is.creatio.com"


def test_wizard_oauth_identity_can_be_overridden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "override.env"
    install_inputs(
        monkeypatch,
        [
            "https://pegadaian.creatio.com",
            "1",
            "1",
            "client",
            "client-secret",
            "https://pegadaian-is.example.com",  # user types their own value
            "",
            "1",
            "2",
        ],
    )
    assert main(["wizard", "--out", str(out)]) == 0
    assert load_env_file(out, override=True)["CREATIO_IDENTITY_URL"] == "https://pegadaian-is.example.com"


def test_wizard_warns_and_can_keep_bad_identity_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A failing probe must warn but still allow keeping the URL."""
    monkeypatch.setattr(
        "csmt.cli._probe_identity_endpoint",
        lambda identity_url, **kwargs: (
            "bad_host",
            "TLS gagal (sertifikat tidak cocok untuk host ini)",
        ),
    )
    out = tmp_path / "warn.env"
    install_inputs(
        monkeypatch,
        [
            "https://host", "1", "1",
            "client", "client-secret",
            "https://wrong_is.example.com",
            "1",  # keep anyway
            "", "1", "2",
        ],
    )
    assert main(["wizard", "--out", str(out)]) == 0
    assert "PERINGATAN" in capsys.readouterr().out
    assert load_env_file(out, override=True)["CREATIO_IDENTITY_URL"] == "https://wrong_is.example.com"


def test_wizard_can_retry_identity_url_after_failed_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            ("bad_host", "HTTP 404 di /connect/token (host ini bukan identity server)"),
            ("token", "token diterima: host dan kredensial valid"),
        ]
    )
    monkeypatch.setattr(
        "csmt.cli._probe_identity_endpoint", lambda url, **kwargs: next(responses)
    )
    out = tmp_path / "retry.env"
    install_inputs(
        monkeypatch,
        [
            "https://host", "1", "1",
            "client", "client-secret",
            "https://wrong.example.com",
            "2",  # enter another URL
            "https://right-is.example.com",
            "", "1", "2",
        ],
    )
    assert main(["wizard", "--out", str(out)]) == 0
    assert load_env_file(out, override=True)["CREATIO_IDENTITY_URL"] == "https://right-is.example.com"


def test_wizard_host_ok_but_credentials_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A real identity server rejecting the client is reported as a credential issue."""
    monkeypatch.setattr(
        "csmt.cli._probe_identity_endpoint",
        lambda url, **kwargs: (
            "host",
            "host identity benar, tetapi kredensial ditolak (HTTP 400: invalid_client)",
        ),
    )
    out = tmp_path / "creds.env"
    install_inputs(
        monkeypatch,
        ["https://host", "1", "1", "client", "wrong-secret",
         "https://host-is.example.com", "", "1", "2"],
    )
    assert main(["wizard", "--out", str(out)]) == 0
    output = capsys.readouterr().out
    assert "Host benar, kredensial belum" in output
    assert "regenerate client secret" in output
    assert load_env_file(out, override=True)["CREATIO_IDENTITY_URL"] == "https://host-is.example.com"


def test_wizard_switches_to_working_odata_path(tmp_path, monkeypatch, capsys) -> None:
    """A path that redirects to login must be replaced by the one that answers OData."""
    responses = iter(
        [
            ("bad_path", "mengembalikan halaman HTML (redirect login)"),
            ("ok", "/0/odata menjawab JSON OData (120 collection)"),
        ]
    )
    monkeypatch.setattr("csmt.cli._probe_odata_path", lambda *a, **kw: next(responses))
    out = tmp_path / "path.env"
    install_inputs(
        monkeypatch,
        ["https://host", "1", "2", "Supervisor", "secret", "", "1", "2"],
    )
    assert main(["wizard", "--out", str(out)]) == 0
    output = capsys.readouterr().out
    assert "memakai /odata" in output
    assert load_env_file(out, override=True)["CREATIO_ODATA_PATH"] == "/odata"


def test_wizard_reports_missing_odata_permission(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "csmt.cli._probe_odata_path",
        lambda *a, **kw: (
            "permission",
            'aktifkan operation "Access to OData" (code CanUseODataService)',
        ),
    )
    out = tmp_path / "perm.env"
    install_inputs(
        monkeypatch,
        ["https://host", "2", "2", "Supervisor", "secret", "", "1", "2"],
    )
    assert main(["wizard", "--out", str(out)]) == 0
    output = capsys.readouterr().out
    assert "akses ditolak" in output
    assert "CanUseODataService" in output
    assert load_env_file(out, override=True)["CREATIO_ODATA_PATH"] == "/odata"


def test_wizard_explains_every_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Each numbered choice must come with a description line (no guessing)."""
    out = tmp_path / "explained.env"
    install_inputs(
        monkeypatch,
        ["https://host", "", "", "client-id", "client-secret",
         "https://host-is.example.com", "", "", "2"],
    )
    assert main(["wizard", "--out", str(out)]) == 0
    output = capsys.readouterr().out

    # runtime: both options explained, .NET Framework marked as default
    assert "Creatio .NET Framework (klasik) -> /0/odata (default)" in output
    assert "Creatio .NET 8 (site baru) -> /odata" in output
    assert "URL OData memakai prefix workspace /0" in output
    # auth: three options explained
    assert "cookie BPMCSRF" in output
    assert "UseCsrfToken=false" in output
    assert "OAuth20IdentityServerUrl" in output
    # TLS: default is "Tidak" and it is explained
    assert "Tidak (menerima sertifikat apa pun) (default)" in output
    assert "self-signed" in output
    assert "Catatan: verifikasi TLS dimatikan" in output
    # test connection choice explained too
    assert "Tidak menulis apa pun" in output

    loaded = load_env_file(out, override=True)
    assert loaded["CREATIO_ODATA_PATH"] == "/0/odata"
    assert loaded["CREATIO_VERIFY_SSL"] == "false"


def test_wizard_secret_prompts_explain_hidden_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Password/secret prompts must warn that nothing is echoed and suggest pasting."""
    out = tmp_path / "secret.env"
    install_inputs(
        monkeypatch,
        ["https://host", "1", "2", "Supervisor", "secret", "", "1", "2"],
    )
    assert main(["wizard", "--out", str(out)]) == 0
    output = capsys.readouterr().out
    assert "Password TIDAK ditampilkan saat diketik" in output
    assert "COPY-PASTE" in output
    assert "jangan mengetik manual" in output


def test_wizard_oauth2_is_the_default_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Pressing Enter at the auth prompt must select OAuth 2.0."""
    out = tmp_path / "default-auth.env"
    install_inputs(
        monkeypatch,
        ["https://host", "", "", "client-id", "client-secret",
         "https://host-is.example.com", "", "", "2"],
    )
    assert main(["wizard", "--out", str(out)]) == 0
    output = capsys.readouterr().out
    assert "1. OAuth 2.0 (client credentials) (default)" in output
    assert "Client secret TIDAK ditampilkan saat diketik" in output
    assert load_env_file(out, override=True)["CREATIO_AUTH_MODE"] == "oauth2"
