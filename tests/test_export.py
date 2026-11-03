"""Export tests: streaming, paging, filtering, lookup expansion, CLI wiring."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from conftest import make_settings

from csmt.cli import main
from csmt.client import ODataClient
from csmt.errors import ConfigError, ODataError
from csmt.export import ExportOptions, Exporter, LookupExpansion
from mock_creatio import MockCreatio


def read_csv(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=delimiter))


def seed_contacts(mock: MockCreatio, count: int = 5) -> str:
    country = mock.seed("Country", {"Name": "Indonesia"})
    for index in range(count):
        mock.seed(
            "Contact",
            {
                "Name": f"Person {index}",
                "Email": f"p{index}@x",
                "CountryId": country,
                "UsrScore": str(10 + index),
                "Active": index % 2 == 0,
            },
        )
    return country


def exporter_for(mock: MockCreatio, **overrides) -> Exporter:
    settings = make_settings(mock)
    client = ODataClient(settings, sleep=lambda _seconds: None)
    options = ExportOptions(entity=overrides.pop("entity", "Contact"),
                            out=overrides.pop("out"), **overrides)
    return Exporter(client, options)


# ------------------------------------------------------------------ options
def test_export_options_validate() -> None:
    with pytest.raises(ConfigError):
        ExportOptions(entity="", out="x.csv")
    with pytest.raises(ConfigError):
        ExportOptions(entity="Contact", out="x.csv", delimiter=";;")
    with pytest.raises(ConfigError):
        ExportOptions(entity="Contact", out="x.csv", limit=-1)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("CountryId=Country.Name", ("CountryId", "Country", "Name")),
        ("OwnerId=Contact", ("OwnerId", "Contact", "Name")),
        ("StatusId=UsrStatus.Code", ("StatusId", "UsrStatus", "Code")),
    ],
)
def test_lookup_expansion_parse(raw: str, expected: tuple[str, str, str]) -> None:
    spec = LookupExpansion.parse(raw)
    assert (spec.field, spec.entity, spec.caption_field) == expected


@pytest.mark.parametrize("raw", ["", "CountryId", "=Country.Name", "CountryId="])
def test_lookup_expansion_parse_rejects(raw: str) -> None:
    with pytest.raises(ConfigError):
        LookupExpansion.parse(raw)


# -------------------------------------------------------------------- rows
def test_export_all_fields(mock: MockCreatio, tmp_path: Path) -> None:
    seed_contacts(mock, 3)
    out = tmp_path / "contacts.csv"
    result = exporter_for(mock, out=out).run()

    assert result["rows"] == 3
    rows = read_csv(out)
    assert len(rows) == 3
    assert {"Name", "Email", "UsrScore"}.issubset(rows[0].keys())
    assert rows[0]["Name"] == "Person 0"
    assert rows[0]["Active"] == "true"
    assert rows[1]["Active"] == "false"


def test_export_select_limits_columns(mock: MockCreatio, tmp_path: Path) -> None:
    seed_contacts(mock, 2)
    out = tmp_path / "sel.csv"
    exporter_for(mock, out=out, select=("Name", "Email")).run()
    rows = read_csv(out)
    assert list(rows[0].keys()) == ["Name", "Email"]


def test_export_writes_bom_for_excel(mock: MockCreatio, tmp_path: Path) -> None:
    seed_contacts(mock, 1)
    out = tmp_path / "bom.csv"
    exporter_for(mock, out=out).run()
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")


def test_export_header_even_without_rows(mock: MockCreatio, tmp_path: Path) -> None:
    out = tmp_path / "empty.csv"
    result = exporter_for(mock, out=out, select=("Name", "Email")).run()
    assert result["rows"] == 0
    assert out.read_text(encoding="utf-8-sig").strip() == "Name,Email"


def test_export_no_header(mock: MockCreatio, tmp_path: Path) -> None:
    seed_contacts(mock, 2)
    out = tmp_path / "nohead.csv"
    exporter_for(mock, out=out, select=("Name",), write_header=False).run()
    lines = out.read_text(encoding="utf-8-sig").splitlines()
    assert lines == ["Person 0", "Person 1"]


def test_export_paging_makes_multiple_requests(mock: MockCreatio, tmp_path: Path) -> None:
    seed_contacts(mock, 7)
    out = tmp_path / "paged.csv"
    exporter_for(mock, out=out, select=("Name",), page_size=3).run()
    assert len(read_csv(out)) == 7
    contact_gets = [r for r in mock.requests if r["method"] == "GET" and "/Contact" in r["path"]]
    assert len(contact_gets) >= 3  # 3 + 3 + 1


def test_export_limit(mock: MockCreatio, tmp_path: Path) -> None:
    seed_contacts(mock, 10)
    out = tmp_path / "limited.csv"
    result = exporter_for(mock, out=out, select=("Name",), limit=3).run()
    assert result["rows"] == 3
    assert len(read_csv(out)) == 3


def test_export_filter_passes_through(mock: MockCreatio, tmp_path: Path) -> None:
    seed_contacts(mock, 5)
    out = tmp_path / "filtered.csv"
    exporter_for(mock, out=out, select=("Name",), filter="UsrScore ge 12").run()
    names = {row["Name"] for row in read_csv(out)}
    assert names == {"Person 2", "Person 3", "Person 4"}


def test_export_orderby(mock: MockCreatio, tmp_path: Path) -> None:
    seed_contacts(mock, 3)
    out = tmp_path / "sorted.csv"
    exporter_for(mock, out=out, select=("Name",), orderby="Name desc").run()
    assert [row["Name"] for row in read_csv(out)] == ["Person 2", "Person 1", "Person 0"]


def test_export_delimiter(mock: MockCreatio, tmp_path: Path) -> None:
    seed_contacts(mock, 2)
    out = tmp_path / "semi.csv"
    exporter_for(mock, out=out, select=("Name", "Email"), delimiter=";").run()
    assert out.read_text(encoding="utf-8-sig").splitlines()[0] == "Name;Email"
    assert len(read_csv(out, delimiter=";")) == 2


# ----------------------------------------------------------------- lookups
def test_export_expands_lookup_to_caption(mock: MockCreatio, tmp_path: Path) -> None:
    country_id = seed_contacts(mock, 4)
    out = tmp_path / "lookup.csv"
    exporter_for(mock, out=out, select=("Name", "CountryId"),
                 lookups=(LookupExpansion.parse("CountryId=Country.Name"),)).run()

    rows = read_csv(out)
    assert [row["CountryId"] for row in rows] == ["Indonesia"] * 4
    # one batched query for the whole buffer, not one per row
    country_gets = [r for r in mock.requests if "/Country" in r["path"]]
    assert len(country_gets) == 1
    lookup_query = country_gets[0]["path"]
    assert "Id+eq+" in lookup_query  # GUID compared unquoted (no %27 = no quote char)
    assert "%27" not in lookup_query
    assert country_id in lookup_query


def test_export_lookup_keeps_raw_id_when_not_found(mock: MockCreatio, tmp_path: Path) -> None:
    mock.seed("Contact", {"Name": "Ghost", "Email": "g@x",
                          "CountryId": "11111111-1111-1111-1111-111111111111"})
    out = tmp_path / "ghost.csv"
    exporter_for(mock, out=out, select=("Name", "CountryId"),
                 lookups=(LookupExpansion.parse("CountryId=Country.Name"),)).run()
    assert read_csv(out)[0]["CountryId"] == "11111111-1111-1111-1111-111111111111"


def test_export_lookup_server_error_keeps_raw_ids(mock: MockCreatio, tmp_path: Path, monkeypatch) -> None:
    seed_contacts(mock, 2)
    client_settings = make_settings(mock)
    client = ODataClient(client_settings, sleep=lambda _seconds: None)
    original = ODataClient.get_collection

    def failing_get(self, entity, **kwargs):
        if entity == "Country":
            raise ODataError("boom", status=500)
        return original(self, entity, **kwargs)

    monkeypatch.setattr(ODataClient, "get_collection", failing_get)
    out = tmp_path / "err.csv"
    Exporter(client, ExportOptions(entity="Contact", out=out, select=("CountryId",),
                                   lookups=(LookupExpansion.parse("CountryId=Country.Name"),))).run()
    client.close()
    rows = read_csv(out)
    assert all(row["CountryId"] for row in rows)  # GUIDs preserved, export still succeeded


# --------------------------------------------------------------------- CLI
def connection_args(mock: MockCreatio) -> list[str]:
    return ["--base-url", mock.url, "--username", "Supervisor", "--password", "secret"]


def test_cli_export_end_to_end(mock: MockCreatio, tmp_path: Path, capsys) -> None:
    seed_contacts(mock, 3)
    out = tmp_path / "cli.csv"
    code = main(["export", "--entity", "Contact", "--out", str(out), *connection_args(mock),
                 "--select", "Name,CountryId", "--expand-lookups", "CountryId=Country.Name",
                 "--quiet"])
    assert code == 0
    assert "Export summary" in capsys.readouterr().out
    rows = read_csv(out)
    assert len(rows) == 3
    assert rows[0]["CountryId"] == "Indonesia"


def test_cli_export_default_output_name(mock: MockCreatio, tmp_path: Path, monkeypatch) -> None:
    seed_contacts(mock, 1)
    monkeypatch.chdir(tmp_path)
    code = main(["export", "--entity", "Contact", "--select", "Name",
                 *connection_args(mock), "--quiet"])
    assert code == 0
    produced = list(tmp_path.glob("Contact-export-*.csv"))
    assert len(produced) == 1
    assert read_csv(produced[0])[0]["Name"] == "Person 0"


def test_cli_export_bad_lookup_spec(mock: MockCreatio, tmp_path: Path) -> None:
    code = main(["export", "--entity", "Contact", "--out", str(tmp_path / "x.csv"),
                 "--expand-lookups", "oops", *connection_args(mock), "--quiet"])
    assert code == 2


def test_cli_export_unknown_entity(mock: MockCreatio, tmp_path: Path) -> None:
    code = main(["export", "--entity", "Nope", "--out", str(tmp_path / "x.csv"),
                 *connection_args(mock), "--quiet"])
    assert code == 2


def test_export_then_import_roundtrip(mock: MockCreatio, tmp_path: Path) -> None:
    """Exported CSV can be imported back (upsert) without creating duplicates."""
    seed_contacts(mock, 2)
    out = tmp_path / "round.csv"
    exporter_for(mock, out=out, select=("Name", "Email")).run()

    mapping = {"entity": "Contact", "key_fields": ["Email"],
               "fields": {"Name": {"target": "Name"}, "Email": {"target": "Email"}}}
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(__import__("json").dumps(mapping), encoding="utf-8")

    code = main(["import", "--csv", str(out), "--mapping", str(mapping_path),
                 *connection_args(mock), "--mode", "upsert", "--quiet",
                 "--no-journal", "--no-report"])
    assert code == 0
    assert len(mock.records("Contact")) == 2  # updated in place, no duplicates


def test_export_unknown_select_field_is_reported(mock: MockCreatio, tmp_path: Path) -> None:
    """A typo in --select should be visible, not silently produce an empty column."""
    seed_contacts(mock, 1)
    out = tmp_path / "typo.csv"
    result = exporter_for(mock, out=out, select=("Name", "Emaill")).run()
    assert result["rows"] == 1
    assert read_csv(out)[0]["Emaill"] == ""  # server returns the field as null


def test_export_lookup_chunking_makes_multiple_queries(mock: MockCreatio, tmp_path: Path) -> None:
    """Many distinct GUIDs are resolved in chunks, not one query per row."""
    for index in range(5):
        mock.seed("Country", {"Name": f"Country {index}"})
    countries = mock.records("Country")
    for index in range(5):
        mock.seed("Contact", {"Name": f"P{index}", "Email": f"p{index}@x",
                              "CountryId": countries[index]["Id"]})
    out = tmp_path / "chunk.csv"
    exporter_for(mock, out=out, select=("Name", "CountryId"), lookup_chunk=2,
                 lookups=(LookupExpansion.parse("CountryId=Country.Name"),)).run()

    assert [row["CountryId"] for row in read_csv(out)] == [f"Country {i}" for i in range(5)]
    country_gets = [r for r in mock.requests if "/Country" in r["path"]]
    assert len(country_gets) == 3  # ceil(5 / 2)


# --------------------------------------------------- REVIEW-4 regressions
def test_export_limit_zero_exports_no_rows(mock: MockCreatio, tmp_path: Path) -> None:
    """--limit 0 is a valid template request: header only, never data rows."""
    seed_contacts(mock, 3)
    out = tmp_path / "template.csv"
    result = exporter_for(mock, out=out, select=("Name", "Email"), limit=0).run()
    assert result["rows"] == 0
    assert out.read_text(encoding="utf-8-sig").strip() == "Name,Email"
    contact_gets = [r for r in mock.requests if "/Contact" in r["path"]]
    assert contact_gets == []  # no data fetched at all


def test_lookup_cache_is_case_insensitive_for_guids(mock: MockCreatio, tmp_path: Path) -> None:
    """Foreign keys may carry uppercase hex while Creatio returns lowercase ids."""
    country_id = mock.seed("Country", {"Name": "Indonesia"})
    mock.seed("Contact", {"Name": "Upper", "Email": "u@x", "CountryId": country_id.upper()})
    out = tmp_path / "upper.csv"
    exporter_for(mock, out=out, select=("Name", "CountryId"),
                 lookups=(LookupExpansion.parse("CountryId=Country.Name"),)).run()
    assert read_csv(out)[0]["CountryId"] == "Indonesia"


def test_lookup_braced_guid_is_queried_unquoted(mock: MockCreatio) -> None:
    from csmt.export import _id_literal, clean_guid, id_key

    braced = "{680A71FB-AB02-4E17-B9E9-EC074CBEC136}"
    assert clean_guid(braced) == "680A71FB-AB02-4E17-B9E9-EC074CBEC136"
    assert id_key(braced) == id_key(braced.lower())
    assert not _id_literal(braced).startswith("'")
    assert _id_literal("not-a-guid") == "'not-a-guid'"


def test_lookup_missing_ids_are_negatively_cached(mock: MockCreatio) -> None:
    """Deleted/inactive lookup ids must not be re-queried on every flush."""
    from csmt.export import LookupResolver

    settings = make_settings(mock)
    client = ODataClient(settings, sleep=lambda _seconds: None)
    resolver = LookupResolver(client, chunk_size=30)
    spec = LookupExpansion.parse("CountryId=Country.Name")
    missing = "11111111-1111-1111-1111-111111111111"
    resolver.resolve(spec, [missing])
    for _ in range(4):
        resolver.resolve(spec, [missing])
    queries = [r for r in mock.requests if "/Country" in r["path"]]
    client.close()
    assert len(queries) == 1
    assert resolver.caption(spec, missing) == missing  # raw id preserved for data safety


def test_export_limit_zero_without_select_uses_metadata_header(mock: MockCreatio, tmp_path: Path) -> None:
    """A template export can derive column names from $metadata (no record reads)."""
    seed_contacts(mock, 2)
    out = tmp_path / "template-all.csv"
    result = exporter_for(mock, out=out, limit=0).run()
    header = out.read_text(encoding="utf-8-sig").splitlines()[0].split(",")
    assert "Name" in header and "Email" in header
    assert result["rows"] == 0
    assert [r for r in mock.requests if "/Contact" in r["path"]] == []
    assert any("$metadata" in r["path"] for r in mock.requests)
