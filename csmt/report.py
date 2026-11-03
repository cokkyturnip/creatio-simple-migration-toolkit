"""Per-row results, the CSV report and the run summary."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SUCCESS_STATUSES = {"created", "updated", "skipped"}
FAILURE_STATUSES = {"failed", "unknown"}

REPORT_COLUMNS = (
    "row",
    "key",
    "action",
    "status",
    "http_status",
    "record_id",
    "error",
)


@dataclass
class RowResult:
    lineno: int
    dedupe_key: str
    action: str
    status: str
    record_id: str | None = None
    http_status: int | None = None
    error: str | None = None
    finished_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def ok(self) -> bool:
        return self.status in SUCCESS_STATUSES


@dataclass
class Summary:
    total: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    planned: int = 0
    failed: int = 0
    unknown: int = 0
    resumed: int = 0
    duration_seconds: float = 0.0
    report_path: str | None = None
    journal_path: str | None = None

    def add(self, result: RowResult, *, resumed: bool = False) -> None:
        self.total += 1
        if resumed:
            self.resumed += 1
        if result.status == "created":
            self.created += 1
        elif result.status == "updated":
            self.updated += 1
        elif result.status == "skipped":
            self.skipped += 1
        elif result.status == "planned":
            self.planned += 1
        elif result.status == "failed":
            self.failed += 1
        elif result.status == "unknown":
            self.unknown += 1

    @property
    def has_failures(self) -> bool:
        return self.failed > 0 or self.unknown > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "planned": self.planned,
            "failed": self.failed,
            "unknown": self.unknown,
            "resumed": self.resumed,
            "duration_seconds": round(self.duration_seconds, 2),
            "report_path": self.report_path,
            "journal_path": self.journal_path,
        }

    def log(self, logger: logging.Logger) -> None:
        logger.info(
            "Done: total=%s created=%s updated=%s skipped=%s planned=%s failed=%s unknown=%s "
            "resumed=%s in %.1fs",
            self.total,
            self.created,
            self.updated,
            self.skipped,
            self.planned,
            self.failed,
            self.unknown,
            self.resumed,
            self.duration_seconds,
        )


class ReportWriter:
    """Streams results to a CSV file and accumulates the summary."""

    def __init__(
        self,
        path: str | Path | None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.log = logger or logging.getLogger(__name__)
        self.path = Path(path).expanduser() if path else None
        self.summary = Summary(report_path=str(self.path) if self.path else None)
        self._handle = None
        self._writer: csv.DictWriter | None = None

    def _ensure_writer(self) -> csv.DictWriter | None:
        if not self.path:
            return None
        if self._writer is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("w", encoding="utf-8", newline="")
            self._writer = csv.DictWriter(self._handle, fieldnames=REPORT_COLUMNS)
            self._writer.writeheader()
        return self._writer

    def add(self, result: RowResult, *, resumed: bool = False) -> None:
        self.summary.add(result, resumed=resumed)
        writer = self._ensure_writer()
        if writer is None:
            return
        writer.writerow(
            {
                "row": result.lineno,
                "key": result.dedupe_key,
                "action": result.action,
                "status": result.status,
                "http_status": result.http_status or "",
                "record_id": result.record_id or "",
                "error": (result.error or "").replace("\n", " ")[:1000],
            }
        )
        if self._handle is not None:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
                self._handle.close()
            finally:
                self._handle = None

    def write_summary_json(self, path: str | Path | None = None) -> Path | None:
        target = Path(path).expanduser() if path else None
        if target is None and self.path is not None:
            target = self.path.with_suffix(".summary.json")
        if target is None:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.summary.as_dict(), indent=2), encoding="utf-8")
        return target

    def __enter__(self) -> "ReportWriter":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def summarise(results: Iterable[RowResult]) -> Summary:
    summary = Summary()
    for result in results:
        summary.add(result)
    return summary


__all__ = [
    "FAILURE_STATUSES",
    "REPORT_COLUMNS",
    "RowResult",
    "ReportWriter",
    "SUCCESS_STATUSES",
    "Summary",
    "summarise",
]
