"""Import template generation from ``$metadata``.

Turns CSDL field details into a CSV or XLSX template, plus an optional starter
mapping. Lookup columns are headed by the navigation name (``Owner``) instead of
the GUID property (``OwnerId``) because the cell holds the record label; the
generated mapping keeps the real OData target and resolves the label through
``lookup.key``.
"""

from __future__ import annotations

import csv
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import CsdlFieldDetail
from .errors import ConfigError

_LOG = logging.getLogger(__name__)

SYSTEM_COLUMNS = (
    "Id",
    "CreatedOn",
    "CreatedById",
    "ModifiedOn",
    "ModifiedById",
    "ProcessListeners",
)

EDM_TO_MAPPING: dict[str, str] = {
    "Edm.String": "string",
    "Edm.Guid": "guid",
    "Edm.Int16": "int",
    "Edm.Int32": "int",
    "Edm.Int64": "int",
    "Edm.Byte": "int",
    "Edm.SByte": "int",
    "Edm.Decimal": "decimal",
    "Edm.Double": "decimal",
    "Edm.Single": "decimal",
    "Edm.Boolean": "bool",
    "Edm.DateTimeOffset": "datetime",
    "Edm.Date": "date",
    "Edm.TimeOfDay": "string",
}

SAMPLES: dict[str, str] = {
    "string": "contoh teks",
    "guid": "00000000-0000-0000-0000-000000000000",
    "int": "1",
    "decimal": "1.5",
    "bool": "true",
    "datetime": "2026-01-31T00:00:00Z",
    "date": "2026-01-31",
}


@dataclass(frozen=True)
class TemplateColumn:
    """One template column and the mapping entry it produces."""

    header: str
    target: str
    edm_type: str
    mapping_type: str
    nullable: bool = True
    max_length: int | None = None
    lookup_entity: str | None = None
    lookup_key: str | None = None
    mapping: dict[str, Any] = field(default_factory=dict)


def edm_to_mapping_type(edm_type: str) -> str:
    """Map an EDM type to the mapping type understood by :mod:`csmt.mapping`."""
    return EDM_TO_MAPPING.get(edm_type, "string")


def sample_value(column: TemplateColumn) -> str:
    """Return a placeholder value appropriate for the column type."""
    if column.lookup_entity:
        return f"contoh {column.lookup_entity}"
    return SAMPLES.get(column.mapping_type, "contoh")


def build_template_columns(
    details: Sequence[CsdlFieldDetail],
    *,
    include_system: bool = False,
    lookup_key: str = "Name",
) -> list[TemplateColumn]:
    """Build template columns, dropping system, stream and duplicate headers."""
    columns: list[TemplateColumn] = []
    used_headers: set[str] = set()

    for detail in details:
        if detail.type == "Edm.Stream":
            continue
        if not include_system and detail.name in SYSTEM_COLUMNS:
            continue

        mapping_type = edm_to_mapping_type(detail.type)
        header = detail.lookup.navigation if detail.lookup else detail.name
        if not header or header in used_headers:
            header = detail.name
        used_headers.add(header)

        if detail.lookup:
            mapping: dict[str, Any] = {
                "type": "lookup",
                "target": detail.name,
                "lookup": {"entity": detail.lookup.entity, "key": lookup_key},
            }
        else:
            mapping = {"type": mapping_type, "target": detail.name}
            if mapping_type == "string" and detail.max_length is not None:
                mapping["max_length"] = detail.max_length

        columns.append(
            TemplateColumn(
                header=header,
                target=detail.name,
                edm_type=detail.type,
                mapping_type="lookup" if detail.lookup else mapping_type,
                nullable=detail.nullable,
                max_length=detail.max_length,
                lookup_entity=detail.lookup.entity if detail.lookup else None,
                lookup_key=lookup_key if detail.lookup else None,
                mapping=mapping,
            )
        )

    return columns


def build_template_mapping(
    columns: Sequence[TemplateColumn],
    *,
    entity: str | None = None,
    key_columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a starter mapping document for the generated template."""
    document: dict[str, Any] = {"fields": {column.header: column.mapping for column in columns}}
    if entity:
        document["entity"] = entity
    if key_columns:
        cleaned = [key.strip() for key in key_columns if key and key.strip()]
        if cleaned:
            document["key_fields"] = cleaned
    return document


def template_format_for(out_path: str | Path) -> str:
    """Infer the output format from the file extension."""
    suffix = Path(out_path).suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix == ".xlsx":
        return "xlsx"
    raise ConfigError(f"--out must end with .csv or .xlsx (got {str(out_path)!r})")


def write_csv_template(
    out_path: str | Path,
    columns: Sequence[TemplateColumn],
    *,
    delimiter: str = ",",
    bom: bool = True,
    sample: bool = False,
) -> None:
    """Write the CSV template: header row plus an optional example row."""
    if not columns:
        raise ConfigError("Template has no columns")

    path = Path(out_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoding = "utf-8-sig" if bom else "utf-8"

    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter, lineterminator="\n")
        writer.writerow([column.header for column in columns])
        if sample:
            writer.writerow([sample_value(column) for column in columns])


def _sheet_title(entity: str) -> str:
    title = re.sub(r"[\[\]:*?/\\]", "_", (entity or "Template")[:31])
    return title or "Template"


def write_xlsx_template(
    out_path: str | Path,
    entity: str,
    columns: Sequence[TemplateColumn],
    *,
    sample: bool = False,
) -> None:
    """Write the XLSX template: data sheet plus a ``schema`` sheet."""
    if not columns:
        raise ConfigError("Template has no columns")

    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = _sheet_title(entity)

    bold = Font(bold=True)
    sheet.append([column.header for column in columns])
    for cell in sheet[1]:
        cell.font = bold
    if sample:
        sheet.append([sample_value(column) for column in columns])

    for index, column in enumerate(columns, start=1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = min(
            max(len(column.header) + 2, 24 if column.lookup_entity else 12), 60
        )
    sheet.freeze_panes = "A2"

    schema = workbook.create_sheet("schema")
    schema.append(["header", "target", "edm_type", "nullable", "max_length", "lookup_entity"])
    for cell in schema[1]:
        cell.font = bold
    for column in columns:
        schema.append(
            [
                column.header,
                column.target,
                column.edm_type,
                "yes" if column.nullable else "no",
                column.max_length if column.max_length is not None else "",
                column.lookup_entity or "",
            ]
        )
    for letter, width in zip("ABCDEF", (24, 24, 24, 10, 12, 28)):
        schema.column_dimensions[letter].width = width

    path = Path(out_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def write_template(
    out_path: str | Path,
    entity: str,
    columns: Sequence[TemplateColumn],
    *,
    sample: bool = False,
    delimiter: str = ",",
    bom: bool = True,
) -> str:
    """Write either CSV or XLSX based on the extension and return the format used."""
    output_format = template_format_for(out_path)
    if output_format == "csv":
        write_csv_template(
            out_path, columns, delimiter=delimiter, bom=bom, sample=sample
        )
    else:
        write_xlsx_template(out_path, entity, columns, sample=sample)
    return output_format


def describe_columns(columns: Sequence[TemplateColumn]) -> Mapping[str, int]:
    """Small summary used by the CLI (total, scalar, lookup)."""
    lookups = sum(1 for column in columns if column.lookup_entity)
    return {"total": len(columns), "scalar": len(columns) - lookups, "lookup": lookups}
