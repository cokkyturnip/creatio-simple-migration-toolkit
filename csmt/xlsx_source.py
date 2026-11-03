"""Read .xlsx/.xlsm workbooks as tabular data.

Uses openpyxl in ``read_only`` mode, so a 100k-row sheet is streamed instead of
being materialised in memory. Cell values are converted to strings with rules
that keep the mapping layer (and therefore CSV imports) working unchanged:

* numbers avoid float artefacts (``1234.56`` never becomes ``1234.5600000001``)
* dates and datetimes become ISO strings
* booleans become ``true``/``false`` (accepted by the ``bool`` converter)
* empty cells become ``""``
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .csv_source import CsvSource  # noqa: F401  (re-exported for callers/tests)
from .errors import CsvError
from .tabular import Row, TableInfo, normalize_header

_LOG = logging.getLogger(__name__)


def cell_to_text(value: Any) -> str:
    """Convert a spreadsheet cell into the string form the mapper expects."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        # A date-only cell appears as datetime at midnight; keep it date-shaped.
        if value.hour == value.minute == value.second == value.microsecond == 0:
            return value.date().isoformat()
        return value.isoformat(sep="T", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, float):
        # Excel stores IEEE doubles and *displays* 15 significant digits. Rounding
        # the same way avoids 0.1+0.2 -> 0.30000000000000004 noise while keeping
        # plain notation (no "1e-07") that the decimal converter reads reliably.
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        return format(Decimal(str(round(value, 15))), "f")
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


class XlsxSource:
    """Iterate a worksheet as :class:`Row` objects (1-based physical row numbers)."""

    def __init__(
        self,
        path: str | Path,
        *,
        sheet: str | None = None,
        header_row: int = 1,
        has_header: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.log = logger or _LOG
        if not self.path.exists():
            raise CsvError(f"Spreadsheet not found: {self.path}")
        if not self.path.is_file():
            raise CsvError(f"Spreadsheet path is not a file: {self.path}")
        if header_row < 1:
            raise CsvError("--header-row must be 1 or greater")
        self.has_header = has_header
        self.header_row = header_row
        self._requested_sheet = sheet
        self._sheet_name: str | None = None
        self._fieldnames: list[str] | None = None
        self._width = 0

    # -------------------------------------------------------------- workbook
    def _load(self):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise CsvError(
                "openpyxl is required for .xlsx support; run 'uv sync'"
            ) from exc
        try:
            return load_workbook(
                filename=str(self.path), read_only=True, data_only=True
            )
        except Exception as exc:  # openpyxl raises a variety of errors
            raise CsvError(f"Cannot open workbook {self.path}: {exc}") from exc

    def _select_sheet(self, workbook: Any) -> Any:
        sheet = self._requested_sheet
        if sheet is None:
            chosen = workbook.worksheets[0] if workbook.worksheets else None
            if chosen is None:
                raise CsvError(f"Workbook has no worksheets: {self.path}")
            self._sheet_name = chosen.title
            return chosen

        names = workbook.sheetnames
        # An exact sheet name always wins: sheets are often named "2024" or "2",
        # and silently treating those as indexes would read the wrong data.
        if isinstance(sheet, str) and sheet in names:
            self._sheet_name = sheet
            return workbook[sheet]
        if isinstance(sheet, str) and sheet.strip().isdigit():
            sheet = int(sheet.strip())
        if isinstance(sheet, int):
            if not 1 <= sheet <= len(names):
                raise CsvError(
                    f"Sheet index {sheet} is out of range; workbook has {len(names)} "
                    f"sheet(s): {', '.join(names)}"
                )
            self._sheet_name = names[sheet - 1]
            return workbook[self._sheet_name]

        if sheet not in names:
            raise CsvError(
                f"Sheet {sheet!r} not found in {self.path.name}; "
                f"available: {', '.join(names)}"
            )
        self._sheet_name = sheet
        return workbook[sheet]

    # ------------------------------------------------------------------ info
    def _read_header(self) -> list[str]:
        if self._fieldnames is not None:
            return self._fieldnames

        workbook = self._load()
        try:
            worksheet = self._select_sheet(workbook)
            header: list[Any] = []
            for row_number, raw in enumerate(
                worksheet.iter_rows(values_only=True), start=1
            ):
                if row_number == self.header_row:
                    header = list(raw)
                    break
            if not header:
                raise CsvError(
                    f"Worksheet {self._sheet_name!r} has no row {self.header_row} to "
                    "use as a header"
                )
            while header and (header[-1] is None or str(header[-1]).strip() == ""):
                header.pop()
            if not header:
                raise CsvError(
                    f"Worksheet {self._sheet_name!r} row {self.header_row} is empty"
                )
            self._width = len(header)
            if self.has_header:
                self._fieldnames = normalize_header(header)
            else:
                self._fieldnames = [f"column_{i}" for i in range(1, self._width + 1)]
            return self._fieldnames
        finally:
            workbook.close()

    @property
    def fieldnames(self) -> list[str]:
        return list(self._read_header())

    @property
    def sheet_name(self) -> str | None:
        if self._sheet_name is None:
            self._read_header()
        return self._sheet_name

    def info(self) -> TableInfo:
        return TableInfo(
            path=self.path,
            format=self.path.suffix.lower().lstrip("."),
            fieldnames=tuple(self.fieldnames),
            sheet=self.sheet_name,
            header_row=self.header_row,
            has_header=self.has_header,
        )

    # ------------------------------------------------------------------ rows
    def iter_rows(self) -> Iterator[Row]:
        fieldnames = self._read_header()
        workbook = self._load()
        try:
            worksheet = self._select_sheet(workbook)
            start = self.header_row if self.has_header else self.header_row - 1
            for row_number, raw in enumerate(
                worksheet.iter_rows(values_only=True), start=1
            ):
                if row_number <= start:
                    continue
                values = list(raw)
                if not values or all(
                    value is None or str(value).strip() == "" for value in values
                ):
                    continue
                if len(values) > len(fieldnames):
                    self.log.debug(
                        "Row %s has %s values but only %s columns; extras are ignored",
                        row_number,
                        len(values),
                        len(fieldnames),
                    )
                yield Row(
                    lineno=row_number,
                    values={
                        name: cell_to_text(values[index] if index < len(values) else None)
                        for index, name in enumerate(fieldnames)
                    },
                )
        finally:
            workbook.close()

    def __iter__(self) -> Iterator[Row]:
        return self.iter_rows()

    def sample_rows(self, limit: int = 5) -> list[Row]:
        rows: list[Row] = []
        for row in self.iter_rows():
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows


__all__ = ["XlsxSource", "cell_to_text"]
