"""CSV reading with encoding/delimiter detection and sane header handling.

Real exports from Excel/Google Sheets are rarely clean: BOMs, semicolon
delimiters, duplicate or empty headers, ragged rows. This module normalises all
of that before the mapping layer sees a row, and keeps the physical line number
so error reports point at the right place.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from .errors import CsvError
from .tabular import Row, TableInfo, normalize_header

_LOG = logging.getLogger(__name__)

CANDIDATE_ENCODINGS: Sequence[str] = ("utf-8-sig", "utf-8", "cp1252", "iso-8859-1")
CANDIDATE_DELIMITERS: Sequence[str] = (",", ";", "\t", "|")
_SAMPLE_BYTES = 65536


def _decode_sample(raw: bytes) -> str:
    """Decode a byte sample, tolerating a truncated multi-byte sequence."""
    for encoding in CANDIDATE_ENCODINGS:
        for trim in range(0, 5):
            chunk = raw[: len(raw) - trim] if trim else raw
            try:
                return chunk.decode(encoding)
            except UnicodeDecodeError:
                continue
    # latin-1 never fails; this is a last resort.
    return raw.decode("iso-8859-1", errors="replace")


def detect_encoding(path: str | Path) -> str:
    """Best-effort encoding detection using the first 64 KiB of the file."""
    file_path = Path(path).expanduser()
    try:
        with file_path.open("rb") as handle:
            raw = handle.read(_SAMPLE_BYTES)
    except OSError as exc:
        raise CsvError(f"Cannot read {file_path}: {exc}") from exc

    if not raw:
        return "utf-8-sig"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for encoding in ("utf-8", "cp1252"):
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "iso-8859-1"


def detect_delimiter(sample_text: str) -> str:
    """Pick the delimiter that yields a consistent, non-trivial field count."""
    lines = [line for line in sample_text.splitlines() if line.strip()][:10]
    if not lines:
        return ","

    scored: list[tuple[float, int, str]] = []
    for delimiter in CANDIDATE_DELIMITERS:
        counts: list[int] = []
        for line in lines:
            try:
                counts.append(len(next(csv.reader([line], delimiter=delimiter, quotechar='"'))))
            except (csv.Error, StopIteration):
                counts.append(0)
        non_zero = [count for count in counts if count > 0]
        if not non_zero:
            continue
        most_common = max(set(non_zero), key=non_zero.count)
        consistency = non_zero.count(most_common) / len(non_zero)
        # Prefer many columns, but never sacrifice consistency for it.
        score = (consistency * 100) + min(most_common, 50)
        scored.append((score, most_common, delimiter))

    if not scored:
        return ","
    scored.sort(reverse=True)
    best = scored[0]
    if best[1] <= 1:
        return ","
    return best[2]


class CsvSource:
    """Iterate a CSV file as :class:`Row` objects."""

    def __init__(
        self,
        path: str | Path,
        *,
        encoding: str | None = None,
        delimiter: str | None = None,
        has_header: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.log = logger or _LOG
        if not self.path.exists():
            raise CsvError(f"CSV file not found: {self.path}")
        if not self.path.is_file():
            raise CsvError(f"CSV path is not a file: {self.path}")
        self.has_header = has_header
        self.encoding = encoding or detect_encoding(self.path)
        self._explicit_delimiter = delimiter
        self._delimiter = delimiter
        self._fieldnames: list[str] | None = None
        self._sample_text: str | None = None

    # ------------------------------------------------------------------ info
    def _read_sample(self) -> str:
        if self._sample_text is None:
            with self.path.open("rb") as handle:
                raw = handle.read(_SAMPLE_BYTES)
            self._sample_text = _decode_sample(raw)
        return self._sample_text

    @property
    def delimiter(self) -> str:
        if self._delimiter is None:
            self._delimiter = self._explicit_delimiter or detect_delimiter(self._read_sample())
        return self._delimiter

    def _first_physical_line(self) -> str:
        with self.path.open("r", encoding=self.encoding, newline="") as handle:
            for line in handle:
                if line.strip():
                    return line
        return ""

    def _load_header(self) -> list[str]:
        if self._fieldnames is not None:
            return self._fieldnames

        first_line = self._first_physical_line()
        try:
            parsed = next(csv.reader([first_line], delimiter=self.delimiter, quotechar='"'))
        except (csv.Error, StopIteration):
            parsed = []
        if not parsed:
            raise CsvError(f"CSV file has no header row: {self.path}")

        if self.has_header:
            self._fieldnames = normalize_header(parsed)
        else:
            # Synthesise column names for header-less files.
            self._fieldnames = [f"column_{i}" for i in range(1, len(parsed) + 1)]
        return self._fieldnames

    @property
    def fieldnames(self) -> list[str]:
        return list(self._load_header())

    def info(self) -> TableInfo:
        return TableInfo(
            path=self.path,
            format="csv",
            encoding=self.encoding,
            delimiter=self.delimiter,
            fieldnames=tuple(self.fieldnames),
            has_header=self.has_header,
        )

    # ------------------------------------------------------------------ rows
    def iter_rows(self) -> Iterator[Row]:
        fieldnames = self._load_header()
        try:
            handle = self.path.open("r", encoding=self.encoding, newline="")
        except OSError as exc:
            raise CsvError(f"Cannot open {self.path}: {exc}") from exc

        with handle:
            reader = csv.reader(handle, delimiter=self.delimiter, quotechar='"')
            if self.has_header:
                try:
                    next(reader)
                except StopIteration:
                    return
            for raw_values in reader:
                lineno = reader.line_num
                if not raw_values or all(not value.strip() for value in raw_values):
                    continue
                if len(raw_values) > len(fieldnames):
                    self.log.debug(
                        "Line %s has %s values but only %s columns; extras are ignored",
                        lineno,
                        len(raw_values),
                        len(fieldnames),
                    )
                values = {
                    name: (raw_values[index] if index < len(raw_values) else "")
                    for index, name in enumerate(fieldnames)
                }
                yield Row(lineno=lineno, values=values)

    def __iter__(self) -> Iterator[Row]:
        return self.iter_rows()

    def sample_rows(self, limit: int = 5) -> list[Row]:
        rows: list[Row] = []
        for row in self.iter_rows():
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows


def iter_csv_dicts(text: str, delimiter: str = ",") -> Iterator[dict[str, str]]:
    """Utility used by tests and ``init-mapping`` to parse in-memory CSV."""
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    for row in reader:
        yield {key: (value or "") for key, value in row.items() if key is not None}


CsvInfo = TableInfo  # backwards-compatible alias

__all__ = [
    "CANDIDATE_DELIMITERS",
    "CANDIDATE_ENCODINGS",
    "CsvInfo",
    "CsvSource",
    "Row",
    "detect_delimiter",
    "detect_encoding",
    "iter_csv_dicts",
    "normalize_header",
    "TableInfo",
]
