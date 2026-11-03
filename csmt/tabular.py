"""One abstraction over tabular files, so the pipeline does not care about format.

`import` and `init-mapping` only need three things from a file: the column names,
an iterator of rows, and the physical position of each row for error reporting.
Both :class:`csmt.csv_source.CsvSource` and :class:`csmt.xlsx_source.XlsxSource`
implement that, and :func:`open_source` picks the right one from the extension.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol, Sequence

from .errors import ConfigError

_LOG = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".csv", ".xlsx", ".xlsm"}
LEGACY_SUFFIX = ".xls"


@dataclass(frozen=True)
class Row:
    """One data record plus its physical position in the file."""

    lineno: int
    values: dict[str, str]


@dataclass(frozen=True)
class TableInfo:
    """What was detected about the file, for logging and reports."""

    path: Path
    format: str
    fieldnames: tuple[str, ...]
    encoding: str | None = None
    delimiter: str | None = None
    sheet: str | None = None
    header_row: int | None = None
    has_header: bool = True

    def describe(self) -> str:
        parts = [f"format={self.format}"]
        if self.encoding:
            parts.append(f"encoding={self.encoding}")
        if self.delimiter:
            parts.append(f"delimiter={self.delimiter!r}")
        if self.sheet:
            parts.append(f"sheet={self.sheet!r}")
        if self.header_row and self.header_row != 1:
            parts.append(f"header_row={self.header_row}")
        parts.append(f"columns={len(self.fieldnames)}")
        return ", ".join(parts)


class TabularSource(Protocol):
    """Minimal interface consumed by the mapping/pipeline layer."""

    path: Path

    @property
    def fieldnames(self) -> list[str]: ...

    def info(self) -> TableInfo: ...

    def iter_rows(self) -> Iterator[Row]: ...


def open_source(
    path: str | Path,
    *,
    encoding: str | None = None,
    delimiter: str | None = None,
    has_header: bool = True,
    sheet: str | None = None,
    header_row: int = 1,
    logger: logging.Logger | None = None,
) -> TabularSource:
    """Open a CSV or XLSX file as a :class:`TabularSource`.

    CSV options (``encoding``, ``delimiter``) are ignored for spreadsheets; the
    caller is expected to warn about that. ``sheet`` accepts a sheet name or a
    1-based index and only applies to spreadsheets.
    """
    suffix = Path(path).expanduser().suffix.lower()
    log = logger or _LOG

    if suffix == LEGACY_SUFFIX:
        raise ConfigError(
            f"{path} is a legacy .xls workbook. Save it as .xlsx first "
            "(Excel: File > Save As > Excel Workbook)."
        )
    if suffix not in SUPPORTED_SUFFIXES:
        raise ConfigError(
            f"Unsupported file type {suffix or '(none)'!r} for {path}. "
            f"Supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )

    if suffix == ".csv":
        if sheet:
            raise ConfigError("--sheet only applies to .xlsx/.xlsm files")
        if header_row != 1:
            raise ConfigError("--header-row only applies to .xlsx/.xlsm files")
        from .csv_source import CsvSource

        return CsvSource(
            path, encoding=encoding, delimiter=delimiter, has_header=has_header, logger=log
        )

    if encoding or delimiter:
        log.warning(
            "--encoding/--delimiter are CSV-only options; ignoring them for %s", suffix
        )
    from .xlsx_source import XlsxSource

    return XlsxSource(
        path,
        sheet=sheet,
        header_row=header_row,
        has_header=has_header,
        logger=log,
    )


def normalize_header(raw_header: Sequence[object]) -> list[str]:
    """Strip BOM/whitespace, fill empty names, make duplicates unique.

    Shared by CSV and spreadsheet readers so both behave identically. A suffix is
    chosen with a loop, so ``["Name", "Name_2", "Name"]`` becomes
    ``["Name", "Name_2", "Name_3"]`` instead of two ``Name_2`` columns.
    """
    names: list[str] = []
    taken: set[str] = set()
    for index, raw in enumerate(raw_header, start=1):
        text = "" if raw is None else str(raw)
        name = text.replace("\ufeff", "").strip()
        if not name:
            name = f"column_{index}"
        candidate = name
        suffix = 2
        while candidate in taken:
            candidate = f"{name}_{suffix}"
            suffix += 1
        taken.add(candidate)
        names.append(candidate)
    return names


__all__ = [
    "LEGACY_SUFFIX",
    "Row",
    "SUPPORTED_SUFFIXES",
    "TableInfo",
    "TabularSource",
    "normalize_header",
    "open_source",
]
