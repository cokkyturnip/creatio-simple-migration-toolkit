"""Export Creatio records to a CSV file.

The inverse of :mod:`csmt.pipeline`. Records are streamed from ``$skip`` paging, so
memory stays flat even for very large entities, and every row is written as soon
as it arrives.

Lookup columns hold GUIDs. With ``--expand-lookups Field=Entity.CaptionField``
those GUIDs are replaced by their display values, resolved in batches and cached
so one lookup entity is queried once per distinct value chunk.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence
from .client import ODataClient, odata_escape
from .errors import ConfigError, ODataError

_LOG = logging.getLogger(__name__)

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def clean_guid(value: Any) -> str | None:
    """Return a brace/space-free GUID string, or ``None`` when the value is not one."""
    text = str(value).strip().strip("{}").strip()
    return text if GUID_RE.match(text) else None


def id_key(value: Any) -> str:
    """Cache key for an id: GUIDs are case-folded, other values are trimmed.

    Creatio returns ids in canonical lowercase while foreign keys in a record can
    carry uppercase hex, so raw string comparison would miss the cache.
    """
    cleaned = clean_guid(value)
    if cleaned is not None:
        return cleaned.casefold()
    return str(value).strip()


def _id_literal(value: str) -> str:
    """GUID keys are compared unquoted; anything else is quoted and escaped."""
    cleaned = clean_guid(value)
    return cleaned if cleaned is not None else f"'{odata_escape(value)}'"


@dataclass(frozen=True)
class LookupExpansion:
    """Replace GUIDs in ``field`` with a caption from ``entity.caption_field``."""

    field: str
    entity: str
    caption_field: str = "Name"
    quote: bool = False

    @classmethod
    def parse(cls, raw: str) -> "LookupExpansion":
        """Parse ``Field=Entity.CaptionField`` (caption defaults to ``Name``)."""
        if "=" not in raw:
            raise ConfigError(
                f"--expand-lookups expects Field=Entity.CaptionField, got {raw!r}"
            )
        source, _, target = raw.partition("=")
        source = source.strip()
        if not source:
            raise ConfigError(f"--expand-lookups is missing the field name in {raw!r}")
        if not target.strip():
            raise ConfigError(f"--expand-lookups is missing the entity in {raw!r}")
        entity, _, caption = target.strip().partition(".")
        entity = entity.strip()
        caption = (caption or "Name").strip()
        if not entity:
            raise ConfigError(f"--expand-lookups has an empty entity in {raw!r}")
        return cls(field=source, entity=entity, caption_field=caption)


@dataclass
class ExportOptions:
    entity: str
    out: Path
    select: Sequence[str] = field(default_factory=tuple)
    filter: str | None = None
    orderby: str | None = None
    limit: int | None = None
    page_size: int | None = None
    delimiter: str = ","
    encoding: str = "utf-8-sig"  # BOM so Excel opens UTF-8 correctly
    write_header: bool = True
    # "auto" resolves from the output suffix (.csv / .xlsx)
    format: str = "auto"
    sheet_name: str | None = None
    lookups: Sequence[LookupExpansion] = field(default_factory=tuple)
    lookup_chunk: int = 30
    log_every: int = 1000

    def __post_init__(self) -> None:
        self.entity = (self.entity or "").strip()
        if not self.entity:
            raise ConfigError("Export needs an entity set name (--entity)")
        if not self.delimiter or len(self.delimiter) != 1:
            raise ConfigError("--delimiter must be a single character")
        if self.limit is not None and self.limit < 0:
            raise ConfigError("--limit must be zero or greater")
        if self.page_size is not None and self.page_size < 1:
            raise ConfigError("--page-size must be greater than zero")
        self.out = Path(self.out).expanduser()

        suffix = self.out.suffix.lower()
        detected = {".csv": "csv", ".xlsx": "xlsx", ".xlsm": "xlsx"}.get(suffix)
        requested = (self.format or "auto").strip().lower()
        if requested == "auto":
            if detected is None:
                raise ConfigError(
                    f"Cannot infer the output format from {self.out.name!r}; "
                    "use --format csv|xlsx or an .csv/.xlsx extension"
                )
            self.format = detected
        else:
            if requested not in {"csv", "xlsx"}:
                raise ConfigError("--format must be csv or xlsx")
            if detected is not None and detected != requested:
                raise ConfigError(
                    f"--format {requested} conflicts with the output name {self.out.name!r}"
                )
            self.format = requested


def _cell(value: Any) -> str:
    """Render a JSON value for a CSV cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, datetime):
        return value.isoformat(sep="T", timespec="seconds")
    if isinstance(value, (date, dt_time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


class LookupResolver:
    """Bulk GUID -> caption resolution with a per-run cache."""

    def __init__(
        self,
        client: ODataClient,
        *,
        chunk_size: int = 30,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.chunk_size = max(1, chunk_size)
        self.log = logger or _LOG
        self._cache: dict[tuple[str, str], dict[str, str]] = {}
        # Ids the server confirmed as non-existent (deleted/inactive lookups).
        # Kept separate from found captions so a missing record still exports its
        # raw GUID instead of silently becoming an empty cell.
        self._missing: dict[tuple[str, str], set[str]] = {}

    def _bucket(self, spec: LookupExpansion) -> dict[str, str]:
        return self._cache.setdefault((spec.entity, spec.caption_field), {})

    def _missing_ids(self, spec: LookupExpansion) -> set[str]:
        return self._missing.setdefault((spec.entity, spec.caption_field), set())

    def resolve(self, spec: LookupExpansion, values: Iterable[Any]) -> None:
        """Fetch captions for all not-yet-cached ids of ``values``."""
        bucket = self._bucket(spec)
        known_missing = self._missing_ids(spec)
        missing: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value in (None, ""):
                continue
            key = id_key(value)
            if key in bucket or key in known_missing or key in seen:
                continue
            seen.add(key)
            missing.append(str(value))

        for start in range(0, len(missing), self.chunk_size):
            chunk = missing[start : start + self.chunk_size]
            filter_text = " or ".join(f"Id eq {_id_literal(value)}" for value in chunk)
            try:
                records, _ = self.client.get_collection(
                    spec.entity,
                    filter=filter_text,
                    select=["Id", spec.caption_field],
                    top=max(len(chunk) * 2, 20),
                )
            except ODataError as exc:
                self.log.warning(
                    "Lookup %s failed (HTTP %s): %s; keeping raw ids",
                    spec.entity,
                    exc.status,
                    exc.detail,
                )
                continue
            returned: set[str] = set()
            for record in records:
                record_id = record.get("Id")
                if record_id is None:
                    continue
                key = id_key(record_id)
                returned.add(key)
                caption = record.get(spec.caption_field)
                bucket[key] = "" if caption is None else str(caption)
            # Negative cache: ids the server did not return (deleted/inactive
            # lookups) must not be queried again on every buffer flush.
            for value in chunk:
                key = id_key(value)
                if key not in returned:
                    known_missing.add(key)

    def caption(self, spec: LookupExpansion, value: Any) -> str | None:
        if value in (None, ""):
            return ""
        key = id_key(value)
        caption = self._bucket(spec).get(key)
        if caption is not None:
            return caption
        # Not resolvable (never queried, or confirmed missing): keep the raw id.
        return str(value)


class RowSink(Protocol):
    """Destination for exported rows (CSV or XLSX)."""

    def write_header(self, fieldnames: Sequence[str]) -> None: ...

    def write_row(self, fieldnames: Sequence[str], values: Mapping[str, Any]) -> None: ...

    def close(self) -> None: ...


class CsvSink:
    """Stream rows into a delimited text file."""

    def __init__(self, options: ExportOptions) -> None:
        self.options = options
        self._handle = options.out.open("w", encoding=options.encoding, newline="")
        self._writer: csv.DictWriter | None = None

    def write_header(self, fieldnames: Sequence[str]) -> None:
        self._writer = csv.DictWriter(
            self._handle,
            fieldnames=list(fieldnames),
            delimiter=self.options.delimiter,
            extrasaction="ignore",
            lineterminator="\n",
        )
        if self.options.write_header:
            self._writer.writeheader()

    def write_row(self, fieldnames: Sequence[str], values: Mapping[str, Any]) -> None:
        assert self._writer is not None
        self._writer.writerow({name: _cell(values.get(name)) for name in fieldnames})

    def close(self) -> None:
        self._handle.flush()
        self._handle.close()


class XlsxSink:
    """Stream rows into an .xlsx workbook using openpyxl's write-only mode.

    Values are written as text on purpose (dates as ISO, decimals as strings) so a
    file exported by csmt imports back identically to the CSV route, without Excel
    locale/date-serial surprises. Integers and floats stay numeric for analysis.
    """

    def __init__(self, options: ExportOptions) -> None:
        from openpyxl import Workbook

        self.options = options
        self._workbook = Workbook(write_only=True)
        title = (options.sheet_name or options.entity or "Export")[:31]
        for char in "[]:*?/\\":
            title = title.replace(char, "_")
        self._sheet = self._workbook.create_sheet(title=title or "Export")
        self._sheet.freeze_panes = "A2"
        self._widths: dict[str, int] = {}

    def write_header(self, fieldnames: Sequence[str]) -> None:
        # write-only mode emits <cols> with the first appended row, so widths must
        # be registered before appending anything (later changes are dropped).
        from openpyxl.utils import get_column_letter

        for index, name in enumerate(fieldnames, start=1):
            self._widths[name] = max(8, min(len(str(name)) + 2, 60))
            self._sheet.column_dimensions[get_column_letter(index)].width = self._widths[name]
        if self.options.write_header:
            self._sheet.append(list(fieldnames))

    def write_row(self, fieldnames: Sequence[str], values: Mapping[str, Any]) -> None:
        # Column widths are fixed in write_header: openpyxl's write-only mode emits
        # the <cols> block with the first appended row, so late changes are ignored.
        row: list[Any] = []
        for name in fieldnames:
            value = values.get(name)
            if isinstance(value, bool):
                cell: Any = value
            elif isinstance(value, (int, float)):
                cell = value
            else:
                cell = _cell(value) or None
            row.append(cell)
        self._sheet.append(row)

    def close(self) -> None:
        # Widths were registered while streaming (see write_header); nothing to
        # mutate here because write-only mode ignores late <cols> changes.
        self._workbook.save(self.options.out)


def _open_sink(options: ExportOptions) -> RowSink:
    return XlsxSink(options) if options.format == "xlsx" else CsvSink(options)


class Exporter:
    """Stream records from Creatio into a CSV file."""

    def __init__(
        self,
        client: ODataClient,
        options: ExportOptions,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.options = options
        self.log = logger or _LOG

    def run(self) -> dict[str, Any]:
        options = self.options
        options.out.parent.mkdir(parents=True, exist_ok=True)
        resolver = LookupResolver(
            self.client, chunk_size=options.lookup_chunk, logger=self.log
        )

        written = 0
        fieldnames: list[str] = list(options.select)
        lookups_by_field = {spec.field: spec for spec in options.lookups}

        if options.limit == 0:
            # Contract: --limit 0 exports no data at all. Still write the header
            # so the file can serve as an empty template. Without --select the
            # column names come from $metadata, which costs no record reads.
            if not fieldnames:
                try:
                    fieldnames = list(self.client.entity_fields(options.entity))
                except ODataError as exc:
                    self.log.warning(
                        "Could not read $metadata to build a template header: %s", exc
                    )
            sink = _open_sink(options)
            try:
                if fieldnames:
                    sink.write_header(fieldnames)
            finally:
                sink.close()
            self.log.info("Exported 0 rows (--limit 0) to %s", options.out)
            return {
                "entity": options.entity,
                "rows": 0,
                "out": str(options.out),
                "format": options.format,
                "columns": fieldnames,
                "lookups": [spec.field for spec in options.lookups],
            }

        sink = _open_sink(options)
        header_written = False
        pending: list[dict[str, Any]] = []
        try:
            for record in self._records():
                if not header_written:
                    if not fieldnames:
                        fieldnames = list(record.keys())
                    unknown = [name for name in fieldnames if name not in record]
                    if unknown:
                        self.log.warning(
                            "Field(s) %s are not present in the first record; "
                            "check --select against the entity schema",
                            unknown,
                        )
                    sink.write_header(fieldnames)
                    header_written = True

                pending.append(record)
                written += 1
                if len(pending) >= 200:
                    self._flush(pending, sink, fieldnames, resolver, lookups_by_field)
                    pending = []
                if options.limit is not None and written >= options.limit:
                    break
                if options.log_every and written % options.log_every == 0:
                    self.log.info("Exported %s rows", written)

            # First record is still buffered when the limit is reached immediately.
            if pending:
                self._flush(pending, sink, fieldnames, resolver, lookups_by_field)
            elif not header_written and fieldnames:
                # No rows matched: still emit the header so the file is usable.
                sink.write_header(fieldnames)
        finally:
            sink.close()

        self.log.info(
            "Exported %s row(s) from %s to %s", written, options.entity, options.out
        )
        return {
            "entity": options.entity,
            "rows": written,
            "out": str(options.out),
            "format": options.format,
            "columns": fieldnames,
            "lookups": [spec.field for spec in options.lookups],
        }

    def _records(self) -> Iterator[dict[str, Any]]:
        options = self.options
        yield from self.client.iter_collection(
            options.entity,
            filter=options.filter,
            select=list(options.select) or None,
            page_size=options.page_size,
            orderby=options.orderby,
        )

    def _flush(
        self,
        rows: Sequence[dict[str, Any]],
        sink: RowSink,
        fieldnames: Sequence[str],
        resolver: LookupResolver,
        lookups_by_field: Mapping[str, LookupExpansion],
    ) -> None:
        # Resolve every lookup column for the whole buffer first, so each lookup
        # entity is queried once per chunk instead of once per row.
        for name, spec in lookups_by_field.items():
            if name not in fieldnames:
                continue
            resolver.resolve(spec, [row.get(name) for row in rows])

        for row in rows:
            values: dict[str, Any] = {}
            for name in fieldnames:
                value = row.get(name)
                spec = lookups_by_field.get(name)
                values[name] = (
                    resolver.caption(spec, value) or ""
                    if spec is not None
                    else value
                )
            sink.write_row(fieldnames, values)


__all__ = ["ExportOptions", "Exporter", "LookupExpansion", "LookupResolver"]
