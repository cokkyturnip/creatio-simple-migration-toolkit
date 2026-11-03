"""Shared pytest fixtures: a mock Creatio server and a pipeline runner."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from csmt.client import ODataClient  # noqa: E402
from csmt.config import Settings  # noqa: E402
from csmt.csv_source import CsvSource  # noqa: E402
from csmt.journal import Journal  # noqa: E402
from csmt.mapping import Mapper, Mapping  # noqa: E402
from csmt.pipeline import ImportPipeline, PipelineOptions  # noqa: E402
from csmt.report import ReportWriter, Summary  # noqa: E402
from mock_creatio import MockCreatio  # noqa: E402


def make_settings(mock: MockCreatio, **overrides: Any) -> Settings:
    data: dict[str, Any] = {
        "base_url": mock.url,
        "username": "Supervisor",
        "password": "secret",
        "entity": "Contact",
        "timeout": 10,
        "max_retries": 2,
        "retry_backoff": 0.001,
        "retry_max_delay": 0.01,
    }
    data.update(overrides)
    return Settings(**data)


@pytest.fixture
def mock() -> MockCreatio:
    server = MockCreatio().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def settings(mock: MockCreatio) -> Settings:
    return make_settings(mock)


@pytest.fixture
def client(settings: Settings):
    odata = ODataClient(settings, sleep=lambda _seconds: None)
    try:
        yield odata
    finally:
        odata.close()


@dataclass
class RunResult:
    summary: Summary
    csv_path: Path
    report_path: Path
    journal_path: Path


@pytest.fixture
def run_import(mock: MockCreatio, tmp_path: Path):
    """Run the full pipeline against the mock server for a CSV string."""

    def _run(
        csv_text: str,
        *,
        name: str = "data",
        mapping: dict[str, Any] | None = None,
        keys: Sequence[str] = (),
        mode: str = "upsert",
        encoding: str = "utf-8",
        **option_overrides: Any,
    ) -> RunResult:
        csv_path = tmp_path / f"{name}.csv"
        csv_path.write_text(csv_text, encoding=encoding)
        source = CsvSource(csv_path)

        if mapping is None:
            mapping_obj = Mapping.identity(
                source.fieldnames, key_fields=list(keys), entity="Contact"
            )
        else:
            mapping_obj = Mapping.from_dict(mapping)
        mapping_obj.entity = mapping_obj.entity or "Contact"

        options = PipelineOptions(mode=mode, **option_overrides)
        report_path = csv_path.with_suffix(".report.csv")
        journal_path = csv_path.with_suffix(".journal.jsonl")

        settings_obj = make_settings(mock)
        odata = ODataClient(settings_obj, sleep=lambda _seconds: None)
        journal = Journal(journal_path)
        report = ReportWriter(report_path)
        pipeline = ImportPipeline(
            settings=settings_obj,
            client=odata,
            mapper=Mapper(mapping_obj),
            source=source,
            journal=journal,
            report=report,
            options=options,
        )
        try:
            summary = pipeline.run()
        finally:
            odata.close()
            journal.close()
            report.close()

        return RunResult(summary, csv_path, report_path, journal_path)

    return _run


def read_report(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
