"""CLI tests: every subcommand runs end to end against the mock server."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import read_report

from csmt.cli import main


def connection_args(mock) -> list[str]:
    return [
        "--base-url",
        mock.url,
        "--username",
        "Supervisor",
        "--password",
        "secret",
    ]


def write_csv(tmp_path: Path, text: str, name: str = "contacts.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_import_command(mock, tmp_path: Path, capsys) -> None:
    csv_path = write_csv(tmp_path, "Name,Email\nAlice,a@x\nBob,b@x\n")
    code = main(
        [
            "import",
            "--csv",
            str(csv_path),
            "--entity",
            "Contact",
            *connection_args(mock),
            "--mode",
            "create",
        ]
    )
    assert code == 0
    assert len(mock.records("Contact")) == 2
    output = capsys.readouterr().out
    assert "Import summary" in output
    assert "created" in output


def test_import_command_failure_exit_code(mock, tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path, "Name,Email\nBAD-REQUEST,a@x\n")
    code = main(
        [
            "import",
            "--csv",
            str(csv_path),
            "--entity",
            "Contact",
            *connection_args(mock),
            "--mode",
            "create",
        ]
    )
    assert code == 1


def test_import_command_dry_run(mock, tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path, "Name,Email\nAlice,a@x\n")
    code = main(
        [
            "import",
            "--csv",
            str(csv_path),
            "--entity",
            "Contact",
            *connection_args(mock),
            "--mode",
            "create",
            "--dry-run",
        ]
    )
    assert code == 0
    assert mock.records("Contact") == []


def test_import_command_with_mapping_and_resume(mock, tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path, "Full Name;Email\nAlice;a@x\n", name="semi.csv")
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(
        json.dumps(
            {
                "entity": "Contact",
                "fields": {
                    "Full Name": {"target": "Name"},
                    "Email": {"target": "Email"},
                },
            }
        ),
        encoding="utf-8",
    )
    args = [
        "import",
        "--csv",
        str(csv_path),
        "--mapping",
        str(mapping_path),
        *connection_args(mock),
        "--key",
        "Email",
        "--mode",
        "upsert",
        "--resume",
    ]
    assert main(args) == 0
    assert main(args) == 0
    assert len(mock.records("Contact")) == 1


def test_import_command_validates_fields(mock, tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path, "Name\nAlice\n")
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(
        json.dumps({"entity": "Contact", "fields": {"Name": {"target": "NotAField"}}}),
        encoding="utf-8",
    )
    code = main(
        [
            "import",
            "--csv",
            str(csv_path),
            "--mapping",
            str(mapping_path),
            *connection_args(mock),
            "--mode",
            "create",
            "--validate-fields",
        ]
    )
    assert code == 2
    assert mock.records("Contact") == []


def test_check_command(mock, capsys) -> None:
    code = main(["check", *connection_args(mock), "--entity", "Contact"])
    assert code == 0
    output = capsys.readouterr().out
    assert "OK: connection" in output
    assert "entity_reachable" in output


def test_check_command_bad_credentials(mock) -> None:
    code = main(
        [
            "check",
            "--base-url",
            mock.url,
            "--username",
            "Supervisor",
            "--password",
            "nope",
        ]
    )
    assert code == 2


def test_schema_command(mock, capsys) -> None:
    code = main(["schema", *connection_args(mock), "--entity", "Contact"])
    assert code == 0
    output = capsys.readouterr().out
    assert "Edm.String" in output


def test_schema_command_json(mock, capsys) -> None:
    code = main(["schema", *connection_args(mock), "--entity", "Contact", "--json"])
    assert code == 0
    fields = json.loads(capsys.readouterr().out)
    assert fields["Name"] == "Edm.String"


def test_init_mapping_command(tmp_path: Path, capsys) -> None:
    csv_path = write_csv(tmp_path, "Id,Tanggal Lahir,Total Amount,Name\n,\n")
    out_path = tmp_path / "mapping.json"
    code = main(["init-mapping", "--csv", str(csv_path), "--out", str(out_path), "--key", "Id"])
    assert code == 0
    document = json.loads(out_path.read_text(encoding="utf-8"))
    assert document["fields"]["Id"]["type"] == "guid"
    assert document["fields"]["Tanggal Lahir"]["type"] == "date"
    assert document["key_fields"] == ["Id"]
    assert "Wrote starter mapping" in capsys.readouterr().out


def test_import_reports_are_written(mock, tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path, "Name,Email\nAlice,a@x\n")
    report_path = tmp_path / "report.csv"
    journal_path = tmp_path / "journal.jsonl"
    code = main(
        [
            "import",
            "--csv",
            str(csv_path),
            "--entity",
            "Contact",
            *connection_args(mock),
            "--mode",
            "create",
            "--report",
            str(report_path),
            "--journal",
            str(journal_path),
        ]
    )
    assert code == 0
    rows = read_report(report_path)
    assert rows[0]["status"] == "created"
    assert journal_path.exists()


def test_explicit_cli_credentials_beat_env_auth_mode(mock, tmp_path: Path, monkeypatch) -> None:
    """A stale CREATIO_AUTH_MODE=oauth2 in .env must not hijack explicit --username/--password."""
    monkeypatch.setenv("CREATIO_AUTH_MODE", "oauth2")
    monkeypatch.setenv("CREATIO_CLIENT_ID", "stale")
    monkeypatch.setenv("CREATIO_CLIENT_SECRET", "stale")
    monkeypatch.setenv("CREATIO_IDENTITY_URL", "https://unreachable.invalid")

    csv_path = write_csv(tmp_path, "Name,Email\nAlice,a@x\n")
    code = main(
        ["import", "--csv", str(csv_path), "--entity", "Contact",
         "--base-url", mock.url, "--username", "Supervisor", "--password", "secret",
         "--mode", "create", "--quiet", "--no-journal", "--no-report"]
    )
    assert code == 0
    assert len(mock.records("Contact")) == 1


def test_explicit_auth_mode_still_wins(mock, tmp_path: Path, monkeypatch) -> None:
    """--auth-mode oauth2 with real client credentials is still honoured."""
    monkeypatch.setenv("CREATIO_IDENTITY_URL", mock.url)
    csv_path = write_csv(tmp_path, "Name,Email\nAlice,a@x\n")
    code = main(
        ["import", "--csv", str(csv_path), "--entity", "Contact", "--base-url", mock.url,
         "--auth-mode", "oauth2", "--client-id", "client", "--client-secret", "client-secret",
         "--mode", "create", "--quiet", "--no-journal", "--no-report"]
    )
    assert code == 0
    assert len(mock.records("Contact")) == 1
