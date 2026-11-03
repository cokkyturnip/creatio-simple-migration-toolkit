"""End-to-end pipeline tests against the mock Creatio server."""

from __future__ import annotations

import json

import pytest
from conftest import read_report

from csmt.errors import ConfigError, MappingError

CONTACTS_CSV = "Name,Email\nAlice,alice@example.com\nBob,bob@example.com\n"


def test_create_mode_batches(run_import, mock) -> None:
    result = run_import(CONTACTS_CSV, mode="create")
    assert result.summary.created == 2
    assert result.summary.failed == 0
    assert {record["Name"] for record in mock.records("Contact")} == {"Alice", "Bob"}


def test_create_mode_without_batch(run_import, mock) -> None:
    result = run_import(CONTACTS_CSV, mode="create", use_batch=False)
    assert result.summary.created == 2
    assert len(mock.records("Contact")) == 2


def test_upsert_creates_then_updates(run_import, mock) -> None:
    first = run_import(CONTACTS_CSV, name="upsert", keys=["Email"])
    assert first.summary.created == 2

    second = run_import(CONTACTS_CSV, name="upsert", keys=["Email"])
    assert second.summary.updated == 2
    assert second.summary.created == 0
    assert len(mock.records("Contact")) == 2


def test_upsert_matches_keys_case_insensitively(run_import, mock) -> None:
    mock.seed("Contact", {"Name": "Old name", "Email": "alice@example.com"})
    result = run_import(
        "Name,Email\nNew name,ALICE@EXAMPLE.COM\n", name="case", keys=["Email"]
    )
    assert result.summary.updated == 1
    assert result.summary.created == 0
    assert len(mock.records("Contact")) == 1
    assert mock.records("Contact")[0]["Name"] == "New name"


def test_update_mode_requires_key(run_import) -> None:
    with pytest.raises(ConfigError):
        run_import("Name\nAlice\n", mode="update")


def test_update_mode_missing_record_fails(run_import, mock) -> None:
    result = run_import("Name,Email\nAlice,ghost@example.com\n", mode="update", keys=["Email"])
    assert result.summary.failed == 1
    assert mock.records("Contact") == []
    report = read_report(result.report_path)
    assert "no record found" in report[0]["error"]


def test_update_mode_create_missing(run_import, mock) -> None:
    result = run_import(
        "Name,Email\nAlice,ghost@example.com\n",
        mode="update",
        keys=["Email"],
        create_missing=True,
    )
    assert result.summary.created == 1
    assert len(mock.records("Contact")) == 1


def test_create_mode_skip_existing(run_import, mock) -> None:
    mock.seed("Contact", {"Name": "Existing", "Email": "alice@example.com"})
    result = run_import(
        "Name,Email\nAlice,alice@example.com\nBob,bob@example.com\n",
        mode="create",
        keys=["Email"],
        skip_existing=True,
    )
    assert result.summary.skipped == 1
    assert result.summary.created == 1
    assert len(mock.records("Contact")) == 2


def test_dry_run_writes_nothing(run_import, mock) -> None:
    result = run_import(CONTACTS_CSV, mode="create", dry_run=True)
    assert result.summary.planned == 2
    assert result.summary.created == 0
    assert mock.records("Contact") == []
    assert not result.journal_path.exists()


def test_duplicate_policy_error(run_import, mock) -> None:
    csv_text = "Name,Email\nAlice,dupe@example.com\nAlicia,dupe@example.com\n"
    result = run_import(csv_text, mode="create", keys=["Email"], duplicate_policy="error")
    assert result.summary.created == 1
    assert result.summary.failed == 1
    assert len(mock.records("Contact")) == 1


def test_duplicate_policy_first_wins(run_import, mock) -> None:
    csv_text = "Name,Email\nAlice,dupe@example.com\nAlicia,dupe@example.com\n"
    result = run_import(csv_text, mode="create", keys=["Email"], duplicate_policy="first_wins")
    assert result.summary.created == 1
    assert result.summary.skipped == 1
    assert mock.records("Contact")[0]["Name"] == "Alice"


def test_duplicate_policy_last_wins_upsert(run_import, mock) -> None:
    csv_text = "Name,Email\nAlice,dupe@example.com\nAlicia,dupe@example.com\n"
    result = run_import(csv_text, mode="upsert", keys=["Email"], duplicate_policy="last_wins")
    assert result.summary.created == 1
    assert result.summary.skipped == 1  # the superseded row is reported
    assert len(mock.records("Contact")) == 1
    assert mock.records("Contact")[0]["Name"] == "Alicia"


def test_lookup_resolution(run_import, mock) -> None:
    country_id = mock.seed("Country", {"Name": "Indonesia"})
    mapping = {
        "entity": "Contact",
        "key_fields": ["Email"],
        "fields": {
            "Name": {"target": "Name"},
            "Email": {"target": "Email"},
            "Negara": {
                "target": "CountryId",
                "type": "lookup",
                "lookup": {"entity": "Country", "key_field": "Name"},
            },
        },
    }
    result = run_import(
        "Name,Email,Negara\nAlice,alice@example.com,INDONESIA\n",
        mapping=mapping,
    )
    assert result.summary.created == 1
    assert mock.records("Contact")[0]["CountryId"] == country_id


def test_lookup_not_found_fails(run_import, mock) -> None:
    mapping = {
        "entity": "Contact",
        "fields": {
            "Name": {"target": "Name"},
            "Negara": {
                "target": "CountryId",
                "type": "lookup",
                "lookup": {"entity": "Country", "key_field": "Name"},
            },
        },
    }
    result = run_import("Name,Negara\nAlice,Atlantis\n", mapping=mapping, mode="create")
    assert result.summary.failed == 1
    assert mock.records("Contact") == []
    assert "Atlantis" in read_report(result.report_path)[0]["error"]


def test_resume_skips_completed_rows(run_import, mock) -> None:
    first = run_import(CONTACTS_CSV, name="resume", keys=["Email"])
    assert first.summary.created == 2

    second = run_import(CONTACTS_CSV, name="resume", keys=["Email"], resume=True)
    assert second.summary.resumed == 2
    assert second.summary.skipped == 2
    assert second.summary.created == 0
    assert len(mock.records("Contact")) == 2


def test_batch_transport_failure_is_reconciled(run_import, mock) -> None:
    mock.force_batch_garbage = True
    result = run_import(CONTACTS_CSV, mode="upsert", keys=["Email"])
    assert result.summary.created == 2
    assert result.summary.unknown == 0
    assert len(mock.records("Contact")) == 2


def test_batch_without_keys_reports_unknown(run_import, mock) -> None:
    mock.force_batch_garbage = True
    result = run_import(CONTACTS_CSV, mode="create")
    assert result.summary.unknown == 2
    assert result.summary.created == 0


def test_batch_partial_failure_continues(run_import, mock) -> None:
    csv_text = "Name,Email\nAlice,a@x\nBAD-REQUEST,b@x\nCarol,c@x\n"
    result = run_import(csv_text, mode="create")
    assert result.summary.created == 2
    assert result.summary.failed == 1
    assert {record["Name"] for record in mock.records("Contact")} == {"Alice", "Carol"}


def test_transient_post_error_is_never_resent(run_import, mock) -> None:
    """A failed create gets exactly one POST attempt; re-runs handle the rest."""
    result = run_import("Name,Email\nTRIGGER-503,a@x\n", mode="create")
    assert result.summary.created == 0
    assert result.summary.failed == 1
    assert mock.attempts["TRIGGER-503"] == 1
    assert mock.records("Contact") == []

    # A later run (or --resume) simply tries the row again with a fresh POST.
    result2 = run_import("Name,Email\nTRIGGER-503,a@x\n", mode="create")
    assert result2.summary.created == 1
    assert mock.attempts["TRIGGER-503"] == 2


def test_fail_fast_stops_after_first_error(run_import, mock) -> None:
    mapping = {
        "entity": "Contact",
        "fields": {
            "Name": {"target": "Name", "required": True},
            "Email": {"target": "Email"},
        },
    }
    result = run_import(
        "Name,Email\n,a@x\nBob,b@x\n",
        mapping=mapping,
        mode="create",
        continue_on_error=False,
    )
    assert result.summary.failed == 1
    assert result.summary.total == 1
    assert mock.records("Contact") == []


def test_max_errors_stops_the_run(run_import, mock) -> None:
    csv_text = "Name,Email\nBAD-REQUEST,a@x\nBAD-REQUEST,b@x\nGood,c@x\n"
    result = run_import(csv_text, mode="create", max_errors=1, use_batch=False)
    assert result.summary.failed == 1
    assert len(mock.records("Contact")) == 0


def test_limit_processes_only_first_rows(run_import, mock) -> None:
    csv_text = "Name,Email\nA,a@x\nB,b@x\nC,c@x\n"
    result = run_import(csv_text, mode="create", limit=2)
    assert result.summary.total == 2
    assert len(mock.records("Contact")) == 2


def test_decimal_and_date_serialisation(run_import, mock) -> None:
    mapping = {
        "entity": "Contact",
        "fields": {
            "Name": {"target": "Name"},
            "Birth": {"target": "BirthDate", "type": "date"},
            "Score": {"target": "UsrScore", "type": "decimal"},
        },
    }
    result = run_import(
        'Name;Birth;Score\nAlice;31/12/2024;"1.234,56"\n',
        mapping=mapping,
        mode="create",
    )
    assert result.summary.created == 1
    record = mock.records("Contact")[0]
    assert record["BirthDate"] == "2024-12-31"
    assert record["UsrScore"] == "1234.56"


def test_report_and_journal_contents(run_import, mock) -> None:
    result = run_import("Name,Email\nAlice,a@x\n", name="artifacts", mode="create")
    rows = read_report(result.report_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "created"
    assert rows[0]["row"] == "2"
    assert rows[0]["record_id"]

    entries = [
        json.loads(line)
        for line in result.journal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entries[0]["status"] == "created"
    assert entries[0]["key"] == "row:2"

    summary_path = result.report_path.with_suffix(".summary.json")
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["created"] == 1


def test_strict_sources_rejects_missing_column(run_import) -> None:
    mapping = {
        "entity": "Contact",
        "fields": {"Name": {"target": "Name"}, "Email": {"target": "Email"}},
    }
    with pytest.raises(MappingError):
        run_import("Name\nAlice\n", mapping=mapping, mode="create", strict_sources=True)


def test_missing_optional_column_is_a_warning(run_import, mock) -> None:
    mapping = {
        "entity": "Contact",
        "fields": {"Name": {"target": "Name"}, "Email": {"target": "Email"}},
    }
    result = run_import("Name\nAlice\n", mapping=mapping, mode="create")
    assert result.summary.created == 1


def test_resume_with_lookup_column_skips_rows(run_import, mock) -> None:
    """A non-key lookup field must not leak into the dedupe key."""
    mock.seed("Country", {"Name": "Indonesia"})
    mapping = {
        "entity": "Contact",
        "key_fields": ["Email"],
        "fields": {
            "Name": {"target": "Name"},
            "Email": {"target": "Email"},
            "Negara": {
                "target": "CountryId",
                "type": "lookup",
                "lookup": {"entity": "Country", "key_field": "Name"},
            },
        },
    }
    csv_text = "Name,Email,Negara\nAlice,alice@example.com,Indonesia\n"
    first = run_import(csv_text, name="resume-lookup", mapping=mapping)
    assert first.summary.created == 1

    second = run_import(csv_text, name="resume-lookup", mapping=mapping, resume=True)
    assert second.summary.resumed == 1
    assert second.summary.created == 0
    assert len(mock.records("Contact")) == 1
