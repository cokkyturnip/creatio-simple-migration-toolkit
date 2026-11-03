"""XLSX support: workbook reading, cell typing, export, and CSV parity."""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

import pytest
from conftest import make_settings
from openpyxl import Workbook, load_workbook

from csmt.cli import main
from csmt.client import ODataClient
from csmt.errors import ConfigError, CsvError
from csmt.export import ExportOptions, Exporter, LookupExpansion
from csmt.tabular import open_source
from csmt.xlsx_source import XlsxSource, cell_to_text
from mock_creatio import MockCreatio


def make_workbook(path: Path, rows: list[list], *, sheet: str = "Data", header: bool = True) -> Path:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    if header:
        for row in rows:
            worksheet.append(row)
    else:
        for row in rows:
            worksheet.append(row)
    workbook.save(path)
    return path


# ------------------------------------------------------------- cell typing
@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ""),
        (True, "true"),
        (False, "false"),
        (42, "42"),
        (87.5, "87.5"),
        (1234.56, "1234.56"),
        (0.1 + 0.2, "0.3"),  # float artefact must not leak into the mapping
        (date(1990, 5, 12), "1990-05-12"),
        (datetime(1990, 5, 12, 8, 15, 0), "1990-05-12T08:15:00"),
        (" teks ", " teks "),
    ],
)
def test_cell_to_text(value: object, expected: str) -> None:
    assert cell_to_text(value) == expected


def test_midnight_datetime_becomes_date() -> None:
    assert cell_to_text(datetime(2024, 1, 31)) == "2024-01-31"


# ------------------------------------------------------------------ reading
def test_read_sheet_by_name(tmp_path: Path) -> None:
    path = make_workbook(tmp_path / "a.xlsx", [["Nama", "Email"], ["Andi", "a@x"]])
    source = XlsxSource(path)
    assert source.fieldnames == ["Nama", "Email"]
    rows = list(source)
    assert rows[0].values == {"Nama": "Andi", "Email": "a@x"}
    assert rows[0].lineno == 2


def test_read_sheet_by_index_and_default(tmp_path: Path) -> None:
    path = make_workbook(tmp_path / "b.xlsx", [["Nama"], ["First"]], sheet="Satu")
    workbook = load_workbook(path)
    second = workbook.create_sheet("Dua")
    second.append(["Nama"])
    second.append(["Second"])
    workbook.save(path)

    assert list(XlsxSource(path))[0].values["Nama"] == "First"
    assert list(XlsxSource(path, sheet="Dua"))[0].values["Nama"] == "Second"
    assert list(XlsxSource(path, sheet="2"))[0].values["Nama"] == "Second"


def test_header_row_offset(tmp_path: Path) -> None:
    path = make_workbook(
        tmp_path / "c.xlsx",
        [["Laporan Bulanan"], ["Nama", "Email"], ["Andi", "a@x"]],
    )
    source = XlsxSource(path, header_row=2)
    assert source.fieldnames == ["Nama", "Email"]
    rows = list(source)
    assert len(rows) == 1
    assert rows[0].lineno == 3


def test_empty_cells_and_ragged_rows(tmp_path: Path) -> None:
    path = make_workbook(
        tmp_path / "d.xlsx", [["Nama", "Email", "Kota"], ["Andi", "a@x"], [None, None, "Bandung"]]
    )
    rows = list(XlsxSource(path))
    assert rows[0].values == {"Nama": "Andi", "Email": "a@x", "Kota": ""}
    assert rows[1].values == {"Nama": "", "Email": "", "Kota": "Bandung"}


def test_blank_rows_are_skipped(tmp_path: Path) -> None:
    path = make_workbook(tmp_path / "e.xlsx", [["Nama"], ["A"], [None], ["B"]])
    rows = list(XlsxSource(path))
    assert [row.values["Nama"] for row in rows] == ["A", "B"]


def test_sheet_not_found(tmp_path: Path) -> None:
    path = make_workbook(tmp_path / "f.xlsx", [["Nama"], ["A"]])
    with pytest.raises(CsvError) as excinfo:
        XlsxSource(path, sheet="TidakAda").fieldnames
    assert "TidakAda" in str(excinfo.value)
    assert "Data" in str(excinfo.value)


def test_unsupported_and_legacy_files(tmp_path: Path) -> None:
    txt = tmp_path / "x.txt"
    txt.write_text("a,b", encoding="utf-8")
    with pytest.raises(ConfigError):
        open_source(txt)
    with pytest.raises(ConfigError) as excinfo:
        open_source(tmp_path / "old.xls")
    assert "Save it as .xlsx" in str(excinfo.value)


def test_open_source_dispatches_by_extension(tmp_path: Path) -> None:
    csv_path = tmp_path / "a.csv"
    csv_path.write_text("Nama\nAndi\n", encoding="utf-8")
    xlsx_path = make_workbook(tmp_path / "a.xlsx", [["Nama"], ["Andi"]])
    assert [row.values["Nama"] for row in open_source(csv_path)] == ["Andi"]
    assert [row.values["Nama"] for row in open_source(xlsx_path)] == ["Andi"]


def test_csv_only_flags_are_ignored_for_xlsx(tmp_path: Path, caplog) -> None:
    """--delimiter/--encoding must not break a workbook import (warn only)."""
    path = make_workbook(tmp_path / "g.xlsx", [["Nama"], ["A"]])
    source = open_source(path, delimiter=";", encoding="cp1252")
    assert [row.values["Nama"] for row in source] == ["A"]
    assert "CSV-only" in caplog.text


def test_sheet_flag_rejected_for_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "h.csv"
    csv_path.write_text("Nama\nA\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        open_source(csv_path, sheet="Data")
    with pytest.raises(ConfigError):
        open_source(csv_path, header_row=2)


# ------------------------------------------------------------------ export
def seed(mock: MockCreatio, count: int = 3) -> str:
    country = mock.seed("Country", {"Name": "Indonesia"})
    for index in range(count):
        mock.seed(
            "Contact",
            {"Name": f"Person {index}", "Email": f"p{index}@x",
             "CountryId": country, "UsrScore": str(10 + index), "Active": index % 2 == 0},
        )
    return country


def exporter(mock: MockCreatio, **overrides) -> Exporter:
    client = ODataClient(make_settings(mock), sleep=lambda _seconds: None)
    options = ExportOptions(entity=overrides.pop("entity", "Contact"),
                            out=overrides.pop("out"), **overrides)
    return Exporter(client, options)


def test_export_xlsx_basic(mock: MockCreatio, tmp_path: Path) -> None:
    seed(mock, 3)
    out = tmp_path / "out.xlsx"
    result = exporter(mock, out=out, select=("Name", "UsrScore")).run()
    assert result["rows"] == 3
    assert result["format"] == "xlsx"

    worksheet = load_workbook(out)["Contact"]
    rows = list(worksheet.iter_rows(values_only=True))
    assert rows[0] == ("Name", "UsrScore")
    assert rows[1] == ("Person 0", "10")
    assert worksheet.freeze_panes == "A2"


def test_export_xlsx_sheet_name(mock: MockCreatio, tmp_path: Path) -> None:
    seed(mock, 1)
    out = tmp_path / "named.xlsx"
    exporter(mock, out=out, select=("Name",), sheet_name="Nasabah").run()
    assert load_workbook(out).sheetnames == ["Nasabah"]


def test_export_xlsx_sheet_name_is_sanitised(mock: MockCreatio, tmp_path: Path) -> None:
    seed(mock, 1)
    out = tmp_path / "bad.xlsx"
    exporter(mock, out=out, select=("Name",), sheet_name="a/b:c*d?e").run()
    assert load_workbook(out).sheetnames == ["a_b_c_d_e"]


def test_export_xlsx_limit_zero_template(mock: MockCreatio, tmp_path: Path) -> None:
    seed(mock, 2)
    out = tmp_path / "template.xlsx"
    result = exporter(mock, out=out, select=("Name", "Email"), limit=0).run()
    assert result["rows"] == 0
    rows = list(load_workbook(out)["Contact"].iter_rows(values_only=True))
    assert rows == [("Name", "Email")]


def test_export_xlsx_expands_lookup(mock: MockCreatio, tmp_path: Path) -> None:
    seed(mock, 2)
    out = tmp_path / "lookup.xlsx"
    exporter(mock, out=out, select=("Name", "CountryId"),
             lookups=(LookupExpansion.parse("CountryId=Country.Name"),)).run()
    rows = list(load_workbook(out)["Contact"].iter_rows(values_only=True))
    assert [row[1] for row in rows[1:]] == ["Indonesia", "Indonesia"]


def test_export_format_conflict_and_unknown_suffix(mock: MockCreatio, tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        ExportOptions(entity="Contact", out=tmp_path / "x.csv", format="xlsx")
    with pytest.raises(ConfigError):
        ExportOptions(entity="Contact", out=tmp_path / "x.dat")
    # explicit --format works even with a non-standard suffix
    assert ExportOptions(entity="Contact", out=tmp_path / "x.dat", format="xlsx").format == "xlsx"


def test_export_xlsx_paging(mock: MockCreatio, tmp_path: Path) -> None:
    seed(mock, 7)
    out = tmp_path / "paged.xlsx"
    exporter(mock, out=out, select=("Name",), page_size=3).run()
    rows = list(load_workbook(out)["Contact"].iter_rows(values_only=True))
    assert len(rows) == 8  # header + 7
    contact_gets = [r for r in mock.requests if "/Contact" in r["path"]]
    assert len(contact_gets) >= 3


# --------------------------------------------------------------------- CLI
def conn(mock: MockCreatio) -> list[str]:
    return ["--base-url", mock.url, "--username", "Supervisor", "--password", "secret"]


def test_cli_export_xlsx(mock: MockCreatio, tmp_path: Path) -> None:
    seed(mock, 2)
    out = tmp_path / "cli.xlsx"
    code = main(["export", "--entity", "Contact", "--out", str(out),
                 "--select", "Name", "--quiet", *conn(mock)])
    assert code == 0
    assert len(list(load_workbook(out)["Contact"].iter_rows())) == 3


def test_cli_import_xlsx_with_sheet_and_header_row(mock: MockCreatio, tmp_path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Data"
    worksheet.append(["Laporan"])
    worksheet.append(["Nama", "Email", "Skor"])
    worksheet.append(["Andi", "andi@x", 87.5])
    path = tmp_path / "cli-import.xlsx"
    workbook.save(path)

    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        '{"entity":"Contact","key_fields":["Email"],"fields":{'
        '"Nama":{"target":"Name"},"Email":{"target":"Email"},'
        '"Skor":{"target":"UsrScore","type":"decimal"}}}',
        encoding="utf-8",
    )
    code = main(["import", "--csv", str(path), "--sheet", "Data", "--header-row", "2",
                 "--mapping", str(mapping), "--mode", "upsert", "--quiet",
                 "--no-journal", "--no-report", *conn(mock)])
    assert code == 0
    record = mock.find("Contact", Email="andi@x")[0]
    assert record["Name"] == "Andi"
    assert record["UsrScore"] == "87.5"


def test_xlsx_export_then_import_roundtrip(mock: MockCreatio, tmp_path: Path) -> None:
    """An exported workbook imports back with identical values (dates, bools, decimals)."""
    country = mock.seed("Country", {"Name": "Indonesia"})
    mock.seed("Contact", {"Name": "Round", "Email": "round@x", "CountryId": country,
                          "UsrScore": "1234.56", "BirthDate": "1990-05-12", "Active": True})
    out = tmp_path / "round.xlsx"
    exporter(mock, out=out, select=("Name", "Email", "UsrScore", "BirthDate", "Active")).run()

    mapping = tmp_path / "m.json"
    mapping.write_text(
        '{"entity":"Contact","key_fields":["Email"],"fields":{'
        '"Name":{"target":"Name"},"Email":{"target":"Email"},'
        '"UsrScore":{"target":"UsrScore","type":"decimal"},'
        '"BirthDate":{"target":"BirthDate","type":"date"},'
        '"Active":{"target":"Active","type":"bool"}}}',
        encoding="utf-8",
    )
    code = main(["import", "--csv", str(out), "--mapping", str(mapping),
                 "--mode", "upsert", "--quiet", "--no-journal", "--no-report", *conn(mock)])
    assert code == 0
    record = mock.find("Contact", Email="round@x")[0]
    assert record["UsrScore"] == "1234.56"
    assert record["BirthDate"] == "1990-05-12"
    assert record["Active"] is True
    assert len(mock.records("Contact")) == 1  # updated, not duplicated


# --------------------------------------------------- REVIEW-6 regressions
def test_init_mapping_from_xlsx(tmp_path: Path, capsys) -> None:
    """init-mapping must accept a workbook (no CSV-only attributes)."""
    from csmt.cli import main as cli_main

    path = make_workbook(
        tmp_path / "init.xlsx", [["Laporan"], ["Nama", "Email"], ["A", "a@x"]]
    )
    out = tmp_path / "mapping.json"
    code = cli_main(["init-mapping", "--csv", str(path), "--sheet", "Data",
                     "--header-row", "2", "--out", str(out), "--key", "Email", "--quiet"])
    assert code == 0
    assert "sheet" in capsys.readouterr().out.lower()
    document = __import__("json").loads(out.read_text(encoding="utf-8"))
    assert document["fields"]["Nama"]["target"] == "Nama"


def test_sheet_named_with_digits_is_selected_by_name(tmp_path: Path) -> None:
    """A sheet literally named "2" or "2024" must not be treated as an index."""
    workbook = Workbook()
    first = workbook.active
    first.title = "Overview"
    first.append(["Nama"])
    first.append(["overview"])
    summary = workbook.create_sheet("Summary")
    summary.append(["Nama"])
    summary.append(["summary"])
    numeric = workbook.create_sheet("2")
    numeric.append(["Nama"])
    numeric.append(["numeric-name"])
    path = tmp_path / "digits.xlsx"
    workbook.save(path)

    assert list(XlsxSource(path, sheet="2"))[0].values["Nama"] == "numeric-name"
    assert list(XlsxSource(path, sheet="Summary"))[0].values["Nama"] == "summary"
    # numeric index still works when no sheet carries that name
    assert list(XlsxSource(path, sheet="1"))[0].values["Nama"] == "overview"


def test_sheet_named_year_is_selectable(tmp_path: Path) -> None:
    workbook = Workbook()
    workbook.active.title = "2024"
    workbook.active.append(["Nama"])
    workbook.active.append(["ok"])
    path = tmp_path / "year.xlsx"
    workbook.save(path)
    assert list(XlsxSource(path, sheet="2024"))[0].values["Nama"] == "ok"


def test_export_xlsx_column_widths_are_written(mock: MockCreatio, tmp_path: Path) -> None:
    """Widths must be registered before the first append (write-only mode)."""
    mock.seed("Contact", {"Name": "A very long person name indeed", "Email": "w@x"})
    out = tmp_path / "widths.xlsx"
    exporter(mock, out=out, select=("Name", "Email")).run()
    worksheet = load_workbook(out)["Contact"]
    widths = [d.width for d in worksheet.column_dimensions.values() if d.width]
    assert len(widths) >= 2
    assert max(widths) > len("Name") + 2


def test_export_xlsx_many_columns(mock: MockCreatio, tmp_path: Path) -> None:
    """More than 26 columns must not produce invalid column letters."""
    wide = {f"F{i}": f"v{i}" for i in range(30)}
    mock.seed("Contact", dict(wide, Name="Wide", Email="w@x"))
    out = tmp_path / "wide.xlsx"
    exporter(mock, out=out, select=tuple(wide)).run()
    worksheet = load_workbook(out)["Contact"]
    header = [cell.value for cell in next(worksheet.iter_rows(min_row=1, max_row=1))]
    assert header[-1] == "F29"
    assert len([d for d in worksheet.column_dimensions.values() if d.width]) >= 26


def test_export_sheet_alias(mock: MockCreatio, tmp_path: Path) -> None:
    """--sheet is accepted by export as an alias of --sheet-name."""
    mock.seed("Contact", {"Name": "Alias", "Email": "al@x"})
    out = tmp_path / "alias.xlsx"
    code = main(["export", "--entity", "Contact", "--out", str(out), "--select", "Name",
                 "--sheet", "Alias", "--quiet", *conn(mock)])
    assert code == 0
    assert load_workbook(out).sheetnames == ["Alias"]
