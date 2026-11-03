"""Append-only progress journal used to resume interrupted imports.

Every processed row is written as one JSON object per line. On ``--resume`` the
pipeline loads the journal and skips rows whose key already reached a terminal
success state, which makes re-running a failed import cheap and safe.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from .report import SUCCESS_STATUSES, RowResult

_LOG = logging.getLogger(__name__)


class Journal:
    """JSONL journal. Appends are flushed so a crash keeps completed rows."""

    def __init__(self, path: str | Path | None, *, logger: logging.Logger | None = None) -> None:
        self.log = logger or _LOG
        self.path = Path(path).expanduser() if path else None
        self._handle = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def load_completed(self) -> dict[int, dict[str, Any]]:
        """Return ``{row_lineno: entry}`` for rows whose LAST outcome was final.

        Indexed by physical row number instead of dedupe key: two CSV rows can
        share one key, and a success for row 1 must never mark row 2 complete.
        The last entry per line wins, so a later failure erases an earlier
        success and ``--resume`` re-runs the row.
        """
        completed: dict[int, dict[str, Any]] = {}
        if not self.path or not self.path.exists():
            return completed
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    self.log.warning(
                        "Journal %s: skipping corrupt line %s", self.path, line_number
                    )
                    continue
                if not isinstance(entry, Mapping):
                    continue
                raw_row = entry.get("row")
                status = str(entry.get("status") or "")
                try:
                    row = int(raw_row)
                except (TypeError, ValueError):
                    continue
                if row and status in SUCCESS_STATUSES:
                    completed[row] = dict(entry)
                elif row and status in {"failed", "unknown", "aborted"}:
                    # A failed retry of the same line voids the earlier success.
                    completed.pop(row, None)
        return completed

    def record(self, result: RowResult) -> None:
        if not self.path:
            return
        if result.status == "planned":
            # Dry runs must not mark rows as completed.
            return
        entry = {
            "ts": result.finished_at,
            "row": result.lineno,
            "key": result.dedupe_key,
            "action": result.action,
            "status": result.status,
            "http_status": result.http_status,
            "record_id": result.record_id,
            "error": result.error,
        }
        handle = self._ensure_handle()
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()

    def _ensure_handle(self):
        if self._handle is None:
            assert self.path is not None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
        return self._handle

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
                self._handle.close()
            finally:
                self._handle = None

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


__all__ = ["Journal"]
