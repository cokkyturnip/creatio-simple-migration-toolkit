"""Unit tests for CSV detection and header normalisation."""

from __future__ import annotations

from pathlib import Path

import pytest

from csmt.csv_source import (
    CsvSource,
    detect_delimiter,
    detect_encoding,
    normalize_header,
)
from csmt.errors import CsvError


def test_detect_delimiter_variants() -> None:
    assert detect_delimiter("Name,Email\nA,a@x\nB,b@x\n") == ","
    assert detect_delimiter("Name;Email\nA;a@x\nB;b@x\n") == ";"
    assert detect_delimiter("Name\tEmail\nA\ta@x\nB\tb@x\n") == "\t"
    assert detect_delimiter("Name|Email\nA|a@x\nB|b@x\n") == "|"
    assert detect_delimiter("") == ","


def test_detect_encoding_bom(tmp_path: Path) -> None:
    path = tmp_path / "bom.csv"
    path.write_bytes("\ufeffName,Email\nA,a@x\n".encode("utf-8"))
    assert detect_encoding(path) == "utf-8-sig"


def test_detect_encoding_cp1252(tmp_path: Path) -> None:
    path = tmp_path / "latin.csv"
    path.write_bytes("Nama,Kota\nAndré,München\n".encode("cp1252"))
    assert detect_encoding(path) in {"cp1252", "iso-8859-1"}


def test_normalize_header_handles_duplicates_and_blanks() -> None:
    assert normalize_header([" Name ", "Email", "Email", ""]) == [
        "Name",
        "Email",
        "Email_2",
        "column_4",
    ]


def test_csv_source_reads_semicolon_bom(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_bytes("\ufeffNama;Email\nAndré;andre@example.com\n".encode("utf-8"))
    source = CsvSource(path)
    info = source.info()
    assert info.delimiter == ";"
    assert info.encoding == "utf-8-sig"
    assert info.fieldnames == ("Nama", "Email")

    rows = list(source)
    assert rows[0].values == {"Nama": "André", "Email": "andre@example.com"}
    assert rows[0].lineno == 2


def test_csv_source_ragged_rows_and_empty_lines(tmp_path: Path) -> None:
    path = tmp_path / "ragged.csv"
    path.write_text("Name,Email,Extra\nA,a@x\nB,b@x,c,d\n\n", encoding="utf-8")
    rows = list(CsvSource(path))
    assert rows[0].values == {"Name": "A", "Email": "a@x", "Extra": ""}
    assert rows[1].values == {"Name": "B", "Email": "b@x", "Extra": "c"}


def test_csv_source_without_header(tmp_path: Path) -> None:
    path = tmp_path / "noheader.csv"
    path.write_text("A,a@x\nB,b@x\n", encoding="utf-8")
    source = CsvSource(path, has_header=False)
    assert source.fieldnames == ["column_1", "column_2"]
    rows = list(source)
    assert len(rows) == 2
    assert rows[1].values["column_1"] == "B"


def test_csv_source_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CsvError):
        CsvSource(tmp_path / "missing.csv")
