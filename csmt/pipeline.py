"""The import orchestrator.

Flow for every chunk of rows:

1. map CSV columns to a Creatio payload (``Mapper.prepare``)
2. resolve lookups in bulk (one ``$filter`` per ~30 unique values, cached)
3. look up existing records by key in bulk (``ExistingIndex``)
4. decide create / update / skip
5. execute either as a JSON ``$batch`` (<=100 ops) or one request at a time
6. record the outcome in the CSV report and the resume journal

Safety properties:

* rows are deduplicated by key before anything is sent to Creatio
* batch failures are reconciled by re-querying the key before deciding to retry
* a failed batch (transport error) never blindly retries creates, because a
  create can have succeeded even when the response was lost
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping as TypingMapping, Sequence

from .client import ODataClient, odata_escape
from .config import Settings
from .csv_source import CsvSource
from .errors import BatchError, ConfigError, MappingError, ODataError, ValidationError
from .journal import Journal
from .mapping import Mapper, PreparedRow
from .report import ReportWriter, RowResult, Summary

_LOG = logging.getLogger(__name__)

MODES = ("create", "update", "upsert")
_DUPLICATE_POLICIES = ("error", "first_wins", "last_wins")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass
class PipelineOptions:
    mode: str = "upsert"
    batch_size: int = 50
    use_batch: bool = True
    dry_run: bool = False
    limit: int | None = None
    create_missing: bool = False
    skip_existing: bool = False
    continue_on_error: bool = True
    duplicate_policy: str = "error"
    resume: bool = False
    strict_sources: bool = False
    progress_every: int = 200
    chunk_rows: int = 500
    max_errors: int = 0

    def __post_init__(self) -> None:
        self.mode = (self.mode or "upsert").strip().lower()
        if self.mode not in MODES:
            raise ConfigError(f"Unknown mode {self.mode!r}; expected one of {list(MODES)}")
        self.duplicate_policy = (self.duplicate_policy or "error").strip().lower()
        if self.duplicate_policy not in _DUPLICATE_POLICIES:
            raise ConfigError(
                f"Unknown duplicate policy {self.duplicate_policy!r}; "
                f"expected one of {list(_DUPLICATE_POLICIES)}"
            )
        if self.batch_size < 1 or self.batch_size > 100:
            raise ConfigError("batch_size must be between 1 and 100 (Creatio limit)")
        self.chunk_rows = max(self.chunk_rows, self.batch_size)


@dataclass
class Operation:
    prepared: PreparedRow
    action: str
    record_id: str | None = None

    @property
    def dedupe_key(self) -> str:
        return self.prepared.key_text


@dataclass
class Decision:
    action: str  # create | update | skip | fail
    record_id: str | None = None
    error: str | None = None


def _normalise(value: Any) -> str:
    return str(value).strip().casefold()


def _looks_like_guid(value: Any) -> bool:
    return bool(_GUID_RE.match(str(value).strip()))


class ExistingIndex:
    """Bulk key -> record id lookups with an in-run cache."""

    def __init__(
        self,
        client: ODataClient,
        entity: str,
        key_fields: Sequence[str],
        *,
        quote_fields: TypingMapping[str, bool] | None = None,
        logger: logging.Logger | None = None,
        chunk_chars: int = 1400,
    ) -> None:
        self.client = client
        self.entity = entity
        self.key_fields = list(key_fields)
        self.log = logger or _LOG
        self.quote_fields = dict(quote_fields or {})
        self.chunk_chars = chunk_chars
        self.cache: dict[tuple[str, ...], str | None] = {}

    def _literal(self, field_name: str, value: Any) -> str:
        quote = self.quote_fields.get(field_name)
        if quote is None:
            quote = not (
                field_name.endswith("Id")
                and (_looks_like_guid(value) or str(value).strip().isdigit())
            )
        if quote:
            return f"'{odata_escape(value)}'"
        return str(value)

    def _clause(self, key_values: TypingMapping[str, Any]) -> str:
        parts = [
            f"{name} eq {self._literal(name, key_values[name])}" for name in self.key_fields
        ]
        return "(" + " and ".join(parts) + ")"

    def _cache_key(self, key_values: TypingMapping[str, Any]) -> tuple[str, ...]:
        return tuple(_normalise(key_values[name]) for name in self.key_fields)

    def _chunks(
        self, rows: Iterable[TypingMapping[str, Any]]
    ) -> Iterator[list[TypingMapping[str, Any]]]:
        chunk: list[TypingMapping[str, Any]] = []
        size = 0
        for row in rows:
            clause = self._clause(row)
            if chunk and (size + len(clause) > self.chunk_chars or len(chunk) >= 30):
                yield chunk
                chunk = []
                size = 0
            chunk.append(row)
            size += len(clause) + 4
        if chunk:
            yield chunk

    def preload(self, rows: Iterable[TypingMapping[str, Any]]) -> None:
        pending: list[TypingMapping[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for row in rows:
            cache_key = self._cache_key(row)
            if cache_key in self.cache or cache_key in seen:
                continue
            seen.add(cache_key)
            pending.append(row)

        for chunk in self._chunks(pending):
            filter_text = " or ".join(self._clause(row) for row in chunk)
            select = ["Id"]
            select.extend(name for name in self.key_fields if name != "Id")
            try:
                items, _ = self.client.get_collection(
                    self.entity,
                    filter=filter_text,
                    select=select,
                    top=max(len(chunk) * 4, 20),
                )
            except ODataError as exc:
                raise ODataError(
                    f"Key lookup failed: {exc.detail}",
                    status=exc.status,
                    code=exc.code,
                    payload=exc.payload,
                    method="GET",
                    url=exc.url,
                ) from exc

            found: dict[tuple[str, ...], str] = {}
            for item in items:
                if "Id" not in item:
                    continue
                record_key = tuple(_normalise(item.get(name, "")) for name in self.key_fields)
                if record_key in found and found[record_key] != item["Id"]:
                    self.log.warning(
                        "Multiple %s records share key %s; using the first one (%s)",
                        self.entity,
                        dict(zip(self.key_fields, record_key)),
                        found[record_key],
                    )
                    continue
                found[record_key] = str(item["Id"])

            for row in chunk:
                self.cache[self._cache_key(row)] = found.get(self._cache_key(row))

    def get(self, key_values: TypingMapping[str, Any]) -> str | None:
        return self.cache.get(self._cache_key(key_values))

    def remember(self, key_values: TypingMapping[str, Any], record_id: str | None) -> None:
        self.cache[self._cache_key(key_values)] = record_id


class ImportPipeline:
    """Runs one CSV file into one Creatio entity set."""

    def __init__(
        self,
        *,
        settings: Settings,
        client: ODataClient,
        mapper: Mapper,
        source: CsvSource,
        journal: Journal,
        report: ReportWriter,
        options: PipelineOptions | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.mapper = mapper
        self.source = source
        self.journal = journal
        self.report = report
        self.options = options or PipelineOptions()
        self.log = logger or _LOG

        self.entity = (mapper.mapping.entity or settings.entity or "").strip()
        if not self.entity:
            raise ConfigError("Entity set name is required (--entity or mapping.entity)")

        self.key_fields = list(mapper.mapping.key_fields)
        # GUID keys and lookup keys (whose value resolves to a GUID) must be
        # written as unquoted OData literals, otherwise Creatio fails the
        # $filter with "Incompatible types 'Edm.Guid' and 'Edm.String'".
        quote_fields = {
            spec.target: (spec.type not in {"guid", "lookup"})
            for spec in mapper.mapping.fields
            if spec.target in self.key_fields
        }
        self.index = (
            ExistingIndex(
                client,
                self.entity,
                self.key_fields,
                quote_fields=quote_fields,
                logger=self.log,
            )
            if self.key_fields
            else None
        )
        self.lookup_cache: dict[tuple[str, str, str], str | None] = {}
        self._seen: dict[str, int] = {}
        self._completed: dict[str, Any] = {}
        self._processed = 0
        self._abort = False
        self._error_count = 0
        # Records created during this run, keyed like ExistingIndex.cache entries.
        # Lets the batch-error reconciliation tell "the POST landed despite the lost
        # response" apart from "the key already existed before the batch".
        self._created_now: dict[tuple[str, ...], str] = {}
        # One-off warning so a shifted/edited CSV with --resume explains itself.
        self._warned_row_shift = False

        self._started = 0.0

    # ------------------------------------------------------------------ run
    def run(self) -> Summary:
        self._started = time.monotonic()
        info = self.source.info()
        self.log.info("Reading %s (%s)", info.path, info.describe())

        missing = self.mapper.mapping.missing_sources(info.fieldnames)
        if missing:
            message = f"CSV is missing column(s) used by the mapping: {missing}"
            if self.options.strict_sources:
                raise MappingError(message)
            self.log.warning(message)

        if self.options.mode in {"update", "upsert"} and not self.key_fields:
            raise ConfigError(
                f"mode '{self.options.mode}' needs key fields (--key or mapping.key_fields)"
            )

        if self.options.resume:
            self._completed = self.journal.load_completed()
            self.log.info("Resume mode: %s row(s) already completed", len(self._completed))

        try:
            self._consume()
        finally:
            self.report.summary.duration_seconds = time.monotonic() - self._started
            self.report.summary.journal_path = (
                str(self.journal.path) if self.journal.path else None
            )

        summary = self.report.summary
        summary.duration_seconds = time.monotonic() - self._started
        self.report.write_summary_json()
        return summary

    def _consume(self) -> None:
        chunk: list[PreparedRow] = []
        chunk_index: dict[str, int] = {}
        rows_read = 0

        for row in self.source.iter_rows():
            if self._abort:
                break
            if self.options.limit is not None and rows_read >= self.options.limit:
                break
            rows_read += 1

            try:
                prepared = self.mapper.prepare(row)
            except ValidationError as exc:
                self._emit(
                    RowResult(
                        lineno=row.lineno,
                        dedupe_key=f"row:{row.lineno}",
                        action="map",
                        status="failed",
                        error=str(exc),
                    )
                )
                continue

            dedupe_key = prepared.key_text

            # Resume is keyed by PHYSICAL row number (so regenerated timestamps
            # and lookup GUIDs still match), but a skip is only safe when the
            # row's CONTENT is unchanged: the journal key must match. A shifted,
            # edited or reordered CSV row does not match and is re-processed
            # (upsert then self-corrects via the key match on the server).
            if self.options.resume and row.lineno in self._completed:
                completed_entry = self._completed[row.lineno]
                if str(completed_entry.get("key") or "") == dedupe_key:
                    # Register the key so later duplicate rows cannot bypass
                    # duplicate-policy just because this row was skipped.
                    self._seen[dedupe_key] = row.lineno
                    self._emit(
                        RowResult(
                            lineno=row.lineno,
                            dedupe_key=dedupe_key,
                            action="resume",
                            status="skipped",
                            error="already completed in a previous run",
                        ),
                        resumed=True,
                    )
                    continue
                if not self._warned_row_shift:
                    self.log.warning(
                        "Journal row %s has content that no longer matches "
                        "(key moved/edited between runs); re-processing it",
                        row.lineno,
                    )
                    self._warned_row_shift = True

            duplicate_of = self._seen.get(dedupe_key)
            replaced = False
            if duplicate_of is not None:
                if self.options.duplicate_policy == "last_wins" and self.options.mode != "create":
                    if dedupe_key in chunk_index:
                        position = chunk_index[dedupe_key]
                        superseded = chunk[position]
                        self._emit(
                            RowResult(
                                lineno=superseded.lineno,
                                dedupe_key=dedupe_key,
                                action="skip",
                                status="skipped",
                                error=(
                                    f"superseded by the row on line {row.lineno} "
                                    "(duplicate-policy=last_wins)"
                                ),
                            )
                        )
                        chunk[position] = prepared
                        replaced = True
                    else:
                        # Already sent in an earlier chunk: upsert updates it in place.
                        self.log.debug(
                            "Duplicate key %s on line %s; last value wins",
                            dedupe_key,
                            row.lineno,
                        )
                elif self.options.duplicate_policy == "last_wins":
                    self._emit(
                        RowResult(
                            lineno=row.lineno,
                            dedupe_key=dedupe_key,
                            action="skip",
                            status="skipped",
                            error=f"duplicate key (first seen on line {duplicate_of}); "
                            "last_wins is not safe in create mode",
                        )
                    )
                    continue
                else:
                    status = "failed" if self.options.duplicate_policy == "error" else "skipped"
                    self._emit(
                        RowResult(
                            lineno=row.lineno,
                            dedupe_key=dedupe_key,
                            action="skip",
                            status=status,
                            error=f"duplicate key in CSV (first seen on line {duplicate_of})",
                        )
                    )
                    continue
            else:
                self._seen[dedupe_key] = row.lineno

            if not replaced:
                chunk_index[dedupe_key] = len(chunk)
                chunk.append(prepared)
            if len(chunk) >= self.options.chunk_rows:
                self._process_chunk(chunk)
                chunk = []
                chunk_index.clear()

        if chunk and not self._abort:
            self._process_chunk(chunk)

    # --------------------------------------------------------------- chunking
    def _process_chunk(self, chunk: list[PreparedRow]) -> None:
        self._resolve_lookups(chunk)

        prepared: list[PreparedRow] = []
        for item in chunk:
            try:
                prepared.append(self.mapper.finalize(item))
            except ValidationError as exc:
                self._emit(
                    RowResult(
                        lineno=item.lineno,
                        dedupe_key=item.key_text,
                        action="map",
                        status="failed",
                        error=str(exc),
                    )
                )

        if not prepared:
            return

        if self._needs_existing_lookup() and self.index is not None:
            try:
                self.index.preload(item.key_query_values for item in prepared)
            except ODataError as exc:
                self.log.error("Could not look up existing records: %s", exc)
                for item in prepared:
                    self._emit(
                        RowResult(
                            lineno=item.lineno,
                            dedupe_key=item.key_text,
                            action="lookup",
                            status="failed",
                            http_status=exc.status,
                            error=f"key lookup failed: {exc.detail}",
                        )
                    )
                return

        operations: list[Operation] = []
        for item in prepared:
            decision = self._decide(item)
            if decision.action == "fail":
                self._emit(
                    RowResult(
                        lineno=item.lineno,
                        dedupe_key=item.key_text,
                        action="update",
                        status="failed",
                        error=decision.error or "record not found",
                    )
                )
                continue
            if decision.action == "skip":
                self._emit(
                    RowResult(
                        lineno=item.lineno,
                        dedupe_key=item.key_text,
                        action="skip",
                        status="skipped",
                        record_id=decision.record_id,
                        error="record already exists (skip-existing)",
                    )
                )
                continue
            if not item.payload:
                self._emit(
                    RowResult(
                        lineno=item.lineno,
                        dedupe_key=item.key_text,
                        action=decision.action,
                        status="failed",
                        error="mapped payload is empty",
                    )
                )
                continue
            operations.append(
                Operation(prepared=item, action=decision.action, record_id=decision.record_id)
            )

        if not operations or self._abort:
            return

        if self.options.dry_run:
            for op in operations:
                self._emit(
                    RowResult(
                        lineno=op.prepared.lineno,
                        dedupe_key=op.dedupe_key,
                        action=op.action,
                        status="planned",
                        record_id=op.record_id,
                    )
                )
            return

        if self.options.use_batch and len(operations) > 1:
            self._execute_batch(operations)
        else:
            for op in operations:
                self._execute_one(op)
                if self._abort:
                    break

    def _needs_existing_lookup(self) -> bool:
        if self.index is None:
            return False
        if self.options.mode in {"update", "upsert"}:
            return True
        return self.options.skip_existing

    def _decide(self, prepared: PreparedRow) -> Decision:
        existing_id = self.index.get(prepared.key_query_values) if self.index else None
        if self.options.mode == "create":
            if existing_id and self.options.skip_existing:
                return Decision("skip", record_id=existing_id)
            return Decision("create")
        if self.options.mode == "update":
            if existing_id:
                return Decision("update", record_id=existing_id)
            if self.options.create_missing:
                return Decision("create")
            return Decision(
                "fail",
                error=(
                    "no record found for key "
                    + ", ".join(
                        f"{name}={value!r}" for name, value in prepared.key_values.items()
                    )
                ),
            )
        # upsert
        if existing_id:
            return Decision("update", record_id=existing_id)
        return Decision("create")

    # --------------------------------------------------------------- lookups
    def _resolve_lookups(self, rows: Sequence[PreparedRow]) -> None:
        groups: dict[tuple[str, str, bool, tuple[str, ...]], list[Any]] = {}
        for row in rows:
            for pending in row.pending:
                key = (pending.entity, pending.key_field, pending.quote, tuple(pending.select))
                groups.setdefault(key, []).append(pending)

        for (entity, key_field, quote, select), items in groups.items():
            unresolved: dict[str, list[Any]] = {}
            for pending in items:
                cache_key = (entity, key_field, _normalise(pending.value))
                if cache_key in self.lookup_cache:
                    pending.resolved_id = self.lookup_cache[cache_key]
                else:
                    unresolved.setdefault(_normalise(pending.value), []).append(pending)

            if not unresolved:
                continue

            found: dict[str, str] = {}
            for chunk in self._chunk_values(list(unresolved)):
                filter_text = " or ".join(
                    f"{key_field} eq " + (f"'{odata_escape(value)}'" if quote else value)
                    for value in chunk
                )
                select_fields = ["Id", key_field] + [
                    name for name in select if name not in {"Id", key_field}
                ]
                try:
                    records, _ = self.client.get_collection(
                        entity,
                        filter=filter_text,
                        select=list(dict.fromkeys(select_fields)),
                        top=max(len(chunk) * 4, 20),
                    )
                except ODataError as exc:
                    # Never cache "not found" on a transport/server error: that
                    # would poison every later row that shares this lookup.
                    # Leave resolved_id = None but skip cache writes; the mapper
                    # reports each affected row with the real HTTP status.
                    self.log.error(
                        "Lookup query on %s.%s failed (HTTP %s): %s",
                        entity,
                        key_field,
                        exc.status,
                        exc.detail,
                    )
                    for value_key, pending_items in unresolved.items():
                        for pending in pending_items:
                            pending.resolved_id = None
                            pending.error_status = exc.status
                            pending.error_message = (
                                f"lookup query {entity}.{key_field} failed: {exc.detail}"
                            )
                    continue
                for record in records:
                    if "Id" not in record:
                        continue
                    key_value = record.get(key_field)
                    if key_value is None:
                        for name in select:
                            if name in record:
                                key_value = record[name]
                                break
                    if key_value is None:
                        continue
                    found.setdefault(_normalise(key_value), str(record["Id"]))

            for value_key, pending_items in unresolved.items():
                record_id = found.get(value_key)
                errored = any(pending.error_status for pending in pending_items)
                if not errored:
                    # Only a completed query result may enter the cache.
                    self.lookup_cache[(entity, key_field, value_key)] = record_id
                for pending in pending_items:
                    pending.resolved_id = record_id
                if errored and record_id is None:
                    continue
                if record_id is None:
                    self.log.warning(
                        "Lookup %s.%s has no match for %r",
                        entity,
                        key_field,
                        pending_items[0].value,
                    )

    @staticmethod
    def _chunk_values(
        values: Sequence[str], *, max_values: int = 30, max_chars: int = 1200
    ) -> Iterator[list[str]]:
        chunk: list[str] = []
        size = 0
        for value in values:
            if chunk and (len(chunk) >= max_values or size + len(value) > max_chars):
                yield chunk
                chunk = []
                size = 0
            chunk.append(value)
            size += len(value) + 20
        if chunk:
            yield chunk

    # ------------------------------------------------------------- execution
    def _execute_one(self, op: Operation) -> None:
        payload = op.prepared.payload
        try:
            if op.action == "create":
                # Snapshot whether this key was already known to be absent, so a
                # failed create can be reconciled factually afterwards.
                was_known_absent = False
                cache_key = None
                if self.index is not None:
                    cache_key = self.index._cache_key(op.prepared.key_query_values)
                    was_known_absent = (
                        cache_key in self.index.cache
                        and self.index.cache[cache_key] is None
                    )
                    self.index.remember(op.prepared.key_query_values, None)
                created = self.client.create(self.entity, payload)
                record_id = None
                if isinstance(created, TypingMapping):
                    record_id = created.get("Id") or created.get("id")
                if self.index is not None:
                    self.index.remember(
                        op.prepared.key_query_values, str(record_id) if record_id else None
                    )
                    if record_id:
                        self._created_now[
                            self.index._cache_key(op.prepared.key_query_values)
                        ] = str(record_id)
                self._emit(
                    RowResult(
                        lineno=op.prepared.lineno,
                        dedupe_key=op.dedupe_key,
                        action="create",
                        status="created",
                        record_id=str(record_id) if record_id else None,
                        http_status=201,
                    )
                )
            else:
                self.client.update(self.entity, op.record_id or "", payload)
                self._emit(
                    RowResult(
                        lineno=op.prepared.lineno,
                        dedupe_key=op.dedupe_key,
                        action="update",
                        status="updated",
                        record_id=op.record_id,
                        http_status=204,
                    )
                )
        except ODataError as exc:
            if op.action == "create":
                known_absent = bool(locals().get("was_known_absent", False))
                reconciled = self._reconcile_failed_create(
                    op, exc, was_known_absent=known_absent
                )
                if reconciled is not None:
                    self._emit(reconciled)
                    return
            self._emit(
                RowResult(
                    lineno=op.prepared.lineno,
                    dedupe_key=op.dedupe_key,
                    action=op.action,
                    status="failed",
                    record_id=op.record_id,
                    http_status=exc.status,
                    error=exc.detail,
                )
            )

    def _reconcile_failed_create(
        self, op: Operation, exc: ODataError, *, was_known_absent: bool
    ) -> RowResult | None:
        """Re-check a failed create against the server instead of guessing.

        A POST that died on a transport error or 5xx may still have been
        applied. If the key now exists AND we knew it was absent beforehand (or
        recorded it as created this run), the create factually succeeded.
        Otherwise the outcome is reported ``unknown`` — never a blind success,
        never a blind duplicate POST.
        """
        if self.index is None or not self.key_fields:
            return None
        cache_key = self.index._cache_key(op.prepared.key_query_values)
        if cache_key in self._created_now:
            return None  # success already recorded this run
        # Evict first: remember(None) would make preload() treat the key as
        # already resolved and skip the server query entirely.
        self.index.cache.pop(cache_key, None)
        try:
            self.index.preload(iter([dict(op.prepared.key_query_values)]))
        except ODataError as lookup_exc:
            self.log.error("Could not reconcile failed create: %s", lookup_exc)
            return None
        existing = self.index.get(op.prepared.key_query_values)
        if not existing:
            return None  # nothing written; the plain "failed" row is accurate
        if cache_key in self._created_now or was_known_absent:
            self._created_now[cache_key] = existing
            return RowResult(
                lineno=op.prepared.lineno,
                dedupe_key=op.dedupe_key,
                action="create",
                status="created",
                record_id=existing,
                http_status=exc.status,
                error="reconciled after failed create",
            )
        return RowResult(
            lineno=op.prepared.lineno,
            dedupe_key=op.dedupe_key,
            action="create",
            status="unknown",
            record_id=existing,
            http_status=exc.status,
            error=(
                "create failed but a record with this key exists; the import "
                "never verified it was absent before, so the outcome is unknown"
            ),
        )

    def _execute_batch(self, operations: Sequence[Operation]) -> None:
        batch_ops: list[dict[str, Any]] = []
        by_id: dict[str, Operation] = {}
        for index, op in enumerate(operations, start=1):
            request_id = str(index)
            by_id[request_id] = op
            if op.action == "create":
                batch_ops.append(
                    {
                        "method": "POST",
                        "url": self.entity,
                        "id": request_id,
                        "body": op.prepared.payload,
                        "headers": {
                            "Content-Type": "application/json;odata=verbose",
                            "Accept": "application/json;odata=verbose",
                            "Prefer": "continue-on-error",
                        },
                    }
                )
            else:
                batch_ops.append(
                    {
                        "method": "PATCH",
                        "url": f"{self.entity}({op.record_id})",
                        "id": request_id,
                        "body": op.prepared.payload,
                        "headers": {
                            "Content-Type": "application/json;odata=verbose",
                            "Accept": "application/json;odata=verbose",
                            "Prefer": "continue-on-error",
                        },
                    }
                )

        for start in range(0, len(batch_ops), self.options.batch_size):
            if self._abort:
                return
            window = batch_ops[start : start + self.options.batch_size]
            # Snapshot which create keys were known to be absent just before
            # this window leaves; recovery uses it to separate "the POST landed"
            # from "the key already existed".
            window_known_absent: set[tuple[str, ...]] = set()
            if self.index is not None:
                for batch_op in window:
                    op = by_id[batch_op["id"]]
                    if op.action != "create":
                        continue
                    cache_key = self.index._cache_key(op.prepared.key_query_values)
                    if cache_key in self.index.cache and self.index.cache[cache_key] is None:
                        window_known_absent.add(cache_key)
            window_ops = [by_id[op["id"]] for op in window]
            try:
                results = self.client.batch(window, continue_on_error=True)
            except BatchError as exc:
                self.log.error("Batch failed: %s", exc)
                exc.pre_absent = window_known_absent  # type: ignore[attr-defined]
                self._recover_from_batch_error(window_ops, exc)
                continue

            retry_ops: list[Operation] = []
            for request_id, op in zip((item["id"] for item in window), window_ops):
                result = results.get(request_id)
                if result is None:
                    self._emit(
                        RowResult(
                            lineno=op.prepared.lineno,
                            dedupe_key=op.dedupe_key,
                            action=op.action,
                            status="unknown",
                            record_id=op.record_id,
                            error="no response entry in $batch response",
                        )
                    )
                    continue

                if result.ok:
                    record_id = result.record_id or op.record_id
                    if self.index is not None:
                        self.index.remember(op.prepared.key_query_values, record_id)
                        if op.action == "create" and record_id:
                            self._created_now[
                                self.index._cache_key(op.prepared.key_query_values)
                            ] = record_id
                    self._emit(
                        RowResult(
                            lineno=op.prepared.lineno,
                            dedupe_key=op.dedupe_key,
                            action=op.action,
                            status="created" if op.action == "create" else "updated",
                            record_id=record_id,
                            http_status=result.status,
                        )
                    )
                    continue

                if (
                    result.status == 404
                    and op.action == "update"
                    and self.options.mode == "upsert"
                ):
                    # The record disappeared between lookup and update; create it.
                    if self.index is not None:
                        self.index.remember(op.prepared.key_query_values, None)
                    retry_ops.append(Operation(op.prepared, "create"))
                    continue

                self._emit(
                    RowResult(
                        lineno=op.prepared.lineno,
                        dedupe_key=op.dedupe_key,
                        action=op.action,
                        status="failed",
                        record_id=op.record_id,
                        http_status=result.status,
                        error=result.error_message,
                    )
                )

            for op in retry_ops:
                self._execute_one(op)
                if self._abort:
                    return

    def _recover_from_batch_error(
        self, operations: Sequence[Operation], exc: BatchError
    ) -> None:
        """Decide what to do when the batch response is missing or unparseable.

        A ``$batch`` may have executed partially even if we never saw the
        response, so creates are never retried blindly. When key fields exist we
        ask Creatio which keys now exist; only the still-missing ones are
        resubmitted. Without keys the outcome is reported as ``unknown``.
        """
        if self.index is None or not self.key_fields:
            for op in operations:
                self._emit(
                    RowResult(
                        lineno=op.prepared.lineno,
                        dedupe_key=op.dedupe_key,
                        action=op.action,
                        status="unknown",
                        record_id=op.record_id,
                        http_status=exc.status,
                        error=f"batch outcome unknown: {exc.detail}",
                    )
                )
            return

        # Pre-state snapshot: which keys were known absent BEFORE the batch, so
        # "now exists" after a lost response proves THIS batch wrote them.
        pre_absent: set[tuple[str, ...]] = set(getattr(exc, "pre_absent", ()) )
        for op in operations:
            if op.action != "create" or self.index is None:
                continue
            cache_key = self.index._cache_key(op.prepared.key_query_values)
            if cache_key in self.index.cache and self.index.cache[cache_key] is None:
                pre_absent.add(cache_key)

        # Force a fresh read: the cache may predate the batch.
        self.index.cache.clear()
        try:
            self.index.preload(op.prepared.key_query_values for op in operations)
        except ODataError as lookup_exc:
            self.log.error("Could not reconcile after batch error: %s", lookup_exc)
            for op in operations:
                self._emit(
                    RowResult(
                        lineno=op.prepared.lineno,
                        dedupe_key=op.dedupe_key,
                        action=op.action,
                        status="unknown",
                        record_id=op.record_id,
                        http_status=exc.status,
                        error=f"batch outcome unknown: {exc.detail}",
                    )
                )
            return

        for op in operations:
            existing = self.index.get(op.prepared.key_query_values)
            if op.action == "create":
                # Create: if the key now exists, the POST arrived despite the lost
                # response. Only claim "created" when this run both attempted the
                # create AND knew the key was absent beforehand (upsert/upsert-key
                # runs preload the index); otherwise the truth is "unknown".
                cache_key = self.index._cache_key(op.prepared.key_query_values)
                if existing:
                    if (
                        cache_key in self._created_now
                        or cache_key in pre_absent
                    ):
                        self._created_now[cache_key] = existing
                        self._emit(
                            RowResult(
                                lineno=op.prepared.lineno,
                                dedupe_key=op.dedupe_key,
                                action="create",
                                status="created",
                                record_id=existing,
                                http_status=exc.status,
                                error="reconciled: record now exists for key",
                            )
                        )
                    else:
                        self._emit(
                            RowResult(
                                lineno=op.prepared.lineno,
                                dedupe_key=op.dedupe_key,
                                action="create",
                                status="unknown",
                                record_id=existing,
                                http_status=exc.status,
                                error=(
                                    "batch outcome unknown: record with this key already "
                                    "existed and this run never verified its absence"
                                ),
                            )
                        )
                    continue
                self._execute_one(Operation(op.prepared, "create"))
            else:
                # Update: PATCH is idempotent and the response was definitely
                # lost, so the write is re-executed against the CURRENT id.
                if not existing:
                    if self.options.create_missing:
                        self._execute_one(Operation(op.prepared, "create"))
                    else:
                        self._emit(
                            RowResult(
                                lineno=op.prepared.lineno,
                                dedupe_key=op.dedupe_key,
                                action="update",
                                status="failed",
                                http_status=exc.status,
                                error="batch outcome unknown: key no longer resolves to a record",
                            )
                        )
                    if self._abort:
                        return
                    continue
                self._execute_one(Operation(op.prepared, "update", existing))
            if self._abort:
                return

    # ---------------------------------------------------------------- output
    def _emit(self, result: RowResult, *, resumed: bool = False) -> None:
        self.report.add(result, resumed=resumed)
        self.journal.record(result)
        self._processed += 1

        if result.status in {"failed", "unknown"}:
            self._error_count += 1
            self.log.warning(
                "Line %s (%s) %s: %s", result.lineno, result.dedupe_key, result.status, result.error
            )
        else:
            self.log.debug(
                "Line %s (%s) %s -> %s",
                result.lineno,
                result.dedupe_key,
                result.action,
                result.status,
            )

        if self.options.progress_every and self._processed % self.options.progress_every == 0:
            summary = self.report.summary
            self.log.info(
                "Progress: %s rows (created=%s updated=%s skipped=%s failed=%s)",
                summary.total,
                summary.created,
                summary.updated,
                summary.skipped,
                summary.failed + summary.unknown,
            )

        if not self.options.continue_on_error and result.status in {"failed", "unknown"}:
            self._abort = True
        elif self.options.max_errors and self._error_count >= self.options.max_errors:
            self.log.error("Reached max_errors=%s; stopping", self.options.max_errors)
            self._abort = True


def build_pipeline(
    *,
    settings: Settings,
    client: ODataClient,
    source: CsvSource,
    mapper: Mapper,
    report: ReportWriter,
    journal: Journal,
    options: PipelineOptions,
    logger: logging.Logger | None = None,
) -> ImportPipeline:
    return ImportPipeline(
        settings=settings,
        client=client,
        mapper=mapper,
        source=source,
        journal=journal,
        report=report,
        options=options,
        logger=logger,
    )


__all__ = [
    "Decision",
    "ExistingIndex",
    "ImportPipeline",
    "MODES",
    "Operation",
    "PipelineOptions",
    "build_pipeline",
]
