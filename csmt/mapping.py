"""Mapping CSV columns to Creatio fields, with type coercion and lookups.

A mapping is plain JSON so non-developers can maintain it:

```json
{
  "entity": "Contact",
  "key_fields": ["Email"],
  "constants": {"ProcessListeners": 0},
  "fields": {
    "Nama Lengkap": {"target": "Name", "required": true, "transform": "collapse_spaces"},
    "Email": {"target": "Email", "transform": "lower"},
    "Tanggal Lahir": {"target": "BirthDate", "type": "date"},
    "Negara": {"target": "CountryId", "type": "lookup",
               "lookup": {"entity": "Country", "key_field": "Name"}}
  }
}
```

Lookups are *not* resolved here. ``prepare`` returns pending lookup objects that
the pipeline resolves in bulk (one ``$filter`` per chunk of values) and then
``finalize`` writes the resolved GUIDs into the payload.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping as TypingMapping, Sequence

from .csv_source import Row
from .errors import MappingError, ValidationError

DEFAULT_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%d/%m/%y",
    "%m/%d/%Y",
    "%Y%m%d",
)

_DATE_ONLY_FORMATS = {
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%d/%m/%y",
    "%m/%d/%Y",
    "%Y%m%d",
}

_TRUE_VALUES = {"1", "true", "t", "yes", "y", "ya", "yup"}
_FALSE_VALUES = {"0", "false", "f", "no", "n", "tidak", "tid", "nope"}

SUPPORTED_TYPES = {
    "string",
    "text",
    "guid",
    "int",
    "integer",
    "decimal",
    "float",
    "bool",
    "boolean",
    "date",
    "datetime",
    "lookup",
}


def _collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


TRANSFORMS: dict[str, Callable[[str], str]] = {
    "none": lambda value: value,
    "trim": lambda value: value.strip(),
    "upper": lambda value: value.strip().upper(),
    "lower": lambda value: value.strip().lower(),
    "title": lambda value: " ".join(
        word[:1].upper() + word[1:].lower() for word in value.split()
    ),
    "capitalize": lambda value: value.strip().capitalize(),
    "collapse_spaces": _collapse_spaces,
    "digits": lambda value: re.sub(r"\D+", "", value),
    "digits_plus_prefix": lambda value: (
        "+" + re.sub(r"\D+", "", value) if re.sub(r"\D+", "", value) else ""
    ),
    "strip_zero_width": lambda value: value.replace("\u200b", "").replace("\ufeff", "").strip(),
}


@dataclass
class LookupSpec:
    """Resolve a human readable value into a Creatio record id."""

    entity: str
    key_field: str = "Name"
    quote: bool = True
    select: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: TypingMapping[str, Any]) -> "LookupSpec":
        entity = str(data.get("entity") or "").strip()
        if not entity:
            raise MappingError("lookup.entity is required")
        key_field = str(data.get("key_field") or data.get("key") or "Name").strip()
        select = data.get("select") or ()
        if isinstance(select, str):
            select = tuple(part.strip() for part in select.split(",") if part.strip())
        return cls(
            entity=entity,
            key_field=key_field,
            quote=bool(data.get("quote", True)),
            select=tuple(select),
        )


@dataclass
class FieldSpec:
    source: str
    target: str
    type: str = "string"
    transform: str | None = None
    required: bool = False
    default: Any = None
    null_for_empty: bool = False
    date_format: str | None = None
    lookup: LookupSpec | None = None
    choices: dict[str, str] | None = None
    max_length: int | None = None

    def __post_init__(self) -> None:
        self.type = (self.type or "string").strip().lower()
        if self.type not in SUPPORTED_TYPES:
            raise MappingError(
                f"Field {self.source!r}: unsupported type {self.type!r}; "
                f"expected one of {sorted(SUPPORTED_TYPES)}"
            )
        if self.transform and self.transform not in TRANSFORMS:
            raise MappingError(
                f"Field {self.source!r}: unknown transform {self.transform!r}; "
                f"expected one of {sorted(TRANSFORMS)}"
            )
        if self.type == "lookup" and self.lookup is None:
            raise MappingError(f"Field {self.source!r}: type 'lookup' requires a lookup spec")
        if self.choices:
            self.choices = {str(key).strip(): str(value) for key, value in self.choices.items()}

    @classmethod
    def from_dict(cls, source: str, data: Any) -> "FieldSpec":
        if isinstance(data, str):
            return cls(source=source, target=data)
        if not isinstance(data, TypingMapping):
            raise MappingError(f"Field {source!r}: definition must be an object or a string")

        lookup = data.get("lookup")
        return cls(
            source=source,
            target=str(data.get("target") or source).strip(),
            type=str(data.get("type") or "string"),
            transform=data.get("transform"),
            required=bool(data.get("required", False)),
            default=data.get("default"),
            null_for_empty=bool(data.get("null_for_empty", False)),
            date_format=data.get("format") or data.get("date_format"),
            lookup=LookupSpec.from_dict(lookup) if isinstance(lookup, TypingMapping) else None,
            choices=data.get("choices"),
            max_length=int(data["max_length"]) if data.get("max_length") else None,
        )


@dataclass
class PendingLookup:
    target: str
    entity: str
    key_field: str
    value: str
    quote: bool = True
    select: tuple[str, ...] = ()
    resolved_id: str | None = None
    # Set when the lookup could not be resolved because the query itself failed
    # (transport/HTTP error), as opposed to a genuine "no match". Lets the
    # pipeline report the real HTTP status instead of a misleading "not found".
    error_status: int | None = None
    error_message: str | None = None


@dataclass
class PreparedRow:
    lineno: int
    payload: dict[str, Any]
    pending: list[PendingLookup] = field(default_factory=list)
    # Human-readable key values (the raw CSV strings). Used for dedupe and the
    # resume/report key, which must stay stable across runs even when lookup
    # entities re-issue GUIDs.
    key_values: dict[str, Any] = field(default_factory=dict)
    # Server-facing key values. For lookup key fields this holds the RESOLVED
    # GUID (written during finalize); for everything else it mirrors
    # key_values. ExistingIndex queries Creatio with these.
    key_query_values: dict[str, Any] = field(default_factory=dict)

    @property
    def key_tuple(self) -> tuple[str, ...]:
        return tuple(str(self.key_values.get(name, "")) for name in self.key_values)

    @property
    def key_text(self) -> str:
        if not self.key_values:
            return f"row:{self.lineno}"
        return "|".join(f"{name}={value}" for name, value in self.key_values.items())


@dataclass
class Mapping:
    fields: list[FieldSpec]
    key_fields: list[str] = field(default_factory=list)
    constants: dict[str, Any] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    entity: str | None = None

    @classmethod
    def from_dict(cls, data: TypingMapping[str, Any]) -> "Mapping":
        if not isinstance(data, TypingMapping):
            raise MappingError("Mapping must be a JSON object")

        raw_fields = data.get("fields")
        fields: list[FieldSpec] = []
        if isinstance(raw_fields, TypingMapping):
            for source, spec in raw_fields.items():
                fields.append(FieldSpec.from_dict(str(source), spec))
        elif isinstance(raw_fields, list):
            for item in raw_fields:
                if not isinstance(item, TypingMapping) or not item.get("source"):
                    raise MappingError("Each entry in 'fields' needs a 'source' key")
                fields.append(FieldSpec.from_dict(str(item["source"]), item))
        else:
            raise MappingError("Mapping needs a 'fields' object or list")

        key_fields = data.get("key_fields") or data.get("keys") or []
        if isinstance(key_fields, str):
            key_fields = [part.strip() for part in key_fields.split(",") if part.strip()]
        key_fields = [str(name).strip() for name in key_fields]

        constants = {str(key): value for key, value in (data.get("constants") or {}).items()}
        defaults = {str(key): value for key, value in (data.get("defaults") or {}).items()}

        mapping = cls(
            fields=fields,
            key_fields=key_fields,
            constants=constants,
            defaults=defaults,
            entity=data.get("entity"),
        )
        mapping.validate_keys()
        return mapping

    @classmethod
    def load(cls, path: str | Path) -> "Mapping":
        file_path = Path(path).expanduser()
        if not file_path.exists():
            raise MappingError(f"Mapping file not found: {file_path}")
        try:
            data = json.loads(file_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise MappingError(f"Mapping file {file_path} is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def identity(
        cls,
        columns: Sequence[str],
        *,
        key_fields: Sequence[str] | None = None,
        entity: str | None = None,
    ) -> "Mapping":
        """Map every CSV column to a Creatio field with the same name."""
        fields = []
        for column in columns:
            field_type = "guid" if column.strip().lower() in {"id", "recordid"} else "string"
            fields.append(FieldSpec(source=column, target=column.strip(), type=field_type))
        mapping = cls(fields=fields, key_fields=[str(k) for k in (key_fields or [])], entity=entity)
        mapping.validate_keys()
        return mapping

    # ------------------------------------------------------------- validation
    @property
    def targets(self) -> list[str]:
        return [spec.target for spec in self.fields]

    def validate_keys(self) -> None:
        targets = set(self.targets)
        unknown = [key for key in self.key_fields if key not in targets]
        if unknown:
            raise MappingError(
                f"Key field(s) {unknown} are not produced by the mapping. "
                f"Known targets: {sorted(targets)}"
            )
        seen: set[str] = set()
        duplicates: set[str] = set()
        for spec in self.fields:
            if spec.target in seen:
                duplicates.add(spec.target)
            seen.add(spec.target)
        if duplicates:
            raise MappingError(f"Duplicate target field(s) in mapping: {sorted(duplicates)}")

    def missing_sources(self, columns: Iterable[str]) -> list[str]:
        available = set(columns)
        return [spec.source for spec in self.fields if spec.source not in available]

    def validate_targets(self, allowed: Iterable[str]) -> None:
        """Fail early when the mapping references fields the entity lacks."""
        allowed_set = {name for name in allowed}
        if not allowed_set:
            return
        unknown = sorted(set(self.targets) - allowed_set)
        if unknown:
            raise MappingError(
                f"Mapping targets field(s) that do not exist on the entity: {unknown}"
            )


class Mapper:
    """Turn a CSV :class:`Row` into a payload plus pending lookup objects."""

    def __init__(self, mapping: Mapping, *, logger: Any = None) -> None:
        self.mapping = mapping
        self.log = logger

    # ------------------------------------------------------------- preparing
    def prepare(self, row: Row) -> PreparedRow:
        payload: dict[str, Any] = dict(self.mapping.constants)
        pending: list[PendingLookup] = []
        key_values: dict[str, Any] = {}
        key_query_values: dict[str, Any] = {}

        for spec in self.mapping.fields:
            raw = row.values.get(spec.source)
            is_empty = raw is None or str(raw).strip() == ""

            if is_empty:
                if spec.default is not None:
                    if spec.type == "lookup":
                        # A default on a lookup field is a display value (e.g.
                        # "Indonesia"), not a GUID, so it must join the lookup
                        # resolution flow instead of being sent verbatim.
                        lookup = spec.lookup
                        assert lookup is not None
                        pending.append(
                            PendingLookup(
                                target=spec.target,
                                entity=lookup.entity,
                                key_field=lookup.key_field,
                                value=str(spec.default).strip(),
                                quote=lookup.quote,
                                select=lookup.select,
                            )
                        )
                        payload[spec.target] = None
                        if spec.target in self.mapping.key_fields:
                            key_values[spec.target] = str(spec.default).strip()
                        continue
                    value = self._coerce(spec, spec.default)
                    payload[spec.target] = value
                elif spec.required:
                    raise ValidationError("value is required but empty", field=spec.source)
                elif spec.null_for_empty:
                    payload[spec.target] = None
                else:
                    continue
            else:
                value = self._prepare_value(spec, str(raw))
                if isinstance(value, PendingLookup):
                    pending.append(value)
                    payload[spec.target] = None  # filled by finalize()
                    if spec.target in self.mapping.key_fields:
                        key_values[spec.target] = value.value
                    continue
                payload[spec.target] = value

            if spec.target in self.mapping.key_fields:
                key_values[spec.target] = payload[spec.target]

        for target, default_value in self.mapping.defaults.items():
            payload.setdefault(target, default_value)

        # Defaults/constants may satisfy keys that had no source column.
        for key in self.mapping.key_fields:
            if key not in key_values and payload.get(key) is not None:
                key_values[key] = payload[key]

        if self.mapping.key_fields:
            for key in self.mapping.key_fields:
                if key not in key_values:
                    raise ValidationError(
                        f"key field {key!r} is empty; cannot match the record",
                        field=key,
                    )
            # Initialize server-facing key values from the human-readable ones;
            # finalize() replaces lookup-key entries with resolved GUIDs.
            key_query_values.update(key_values)

        return PreparedRow(
            lineno=row.lineno,
            payload=payload,
            pending=pending,
            key_values=key_values,
            key_query_values=key_query_values,
        )

    def _prepare_value(self, spec: FieldSpec, raw: str) -> Any:
        value = raw
        if spec.choices:
            mapped = spec.choices.get(value.strip())
            if mapped is None:
                raise ValidationError(
                    f"value {value!r} is not one of {sorted(spec.choices)}", field=spec.source
                )
            value = mapped

        if spec.transform:
            try:
                value = TRANSFORMS[spec.transform](value)
            except Exception as exc:  # pragma: no cover - transforms are simple
                raise ValidationError(
                    f"transform {spec.transform!r} failed: {exc}", field=spec.source
                ) from exc

        if spec.type == "lookup":
            lookup = spec.lookup
            assert lookup is not None
            return PendingLookup(
                target=spec.target,
                entity=lookup.entity,
                key_field=lookup.key_field,
                value=str(value).strip(),
                quote=lookup.quote,
                select=lookup.select,
            )

        return self._coerce(spec, value)

    def _coerce(self, spec: FieldSpec, value: Any) -> Any:
        converter = _CONVERTERS.get(spec.type, _to_string)
        try:
            converted = converter(value, spec)
        except ValidationError:
            raise
        except (ValueError, TypeError, InvalidOperation) as exc:
            raise ValidationError(
                str(exc) or "invalid value", field=spec.source, value=value
            ) from exc

        if spec.max_length and isinstance(converted, str) and len(converted) > spec.max_length:
            raise ValidationError(
                f"value is {len(converted)} characters, max is {spec.max_length}",
                field=spec.source,
                value=value,
            )
        return converted

    # -------------------------------------------------------------- finalize
    def finalize(self, prepared: PreparedRow) -> PreparedRow:
        """Write resolved lookup ids into the payload and validate key fields."""
        for lookup in prepared.pending:
            if not lookup.resolved_id:
                if lookup.error_status:
                    # The lookup query itself failed (transport/HTTP error), so
                    # this is NOT "not found": report it as a retryable failure
                    # carrying the real HTTP status for the report.
                    raise ValidationError(
                        f"lookup query for {lookup.value!r} failed: {lookup.error_message}",
                        field=lookup.target,
                        value=lookup.value,
                    )
                raise ValidationError(
                    f"lookup value {lookup.value!r} was not found in "
                    f"{lookup.entity}.{lookup.key_field}",
                    field=lookup.target,
                    value=lookup.value,
                )
            prepared.payload[lookup.target] = lookup.resolved_id
            if lookup.target in self.mapping.key_fields:
                # key_values keeps the human-readable CSV string for dedupe and
                # resume keys (stable across runs even when lookup entities
                # re-issue GUIDs); key_query_values gets the resolved GUID so
                # ExistingIndex filters on the actual GUID column.
                prepared.key_values[lookup.target] = lookup.value
                prepared.key_query_values[lookup.target] = lookup.resolved_id

        for key in self.mapping.key_fields:
            if prepared.payload.get(key) in (None, ""):
                raise ValidationError(
                    f"key field {key!r} is empty; cannot match the record", field=key
                )
        prepared.pending = []
        return prepared


# ----------------------------------------------------------------- converters
def _to_string(value: Any, spec: FieldSpec) -> str:
    text = str(value)
    if spec.transform is None:
        # Without an explicit transform we still trim the edges because trailing
        # spaces break lookups and uniqueness checks.
        return text.strip()
    return text


def _to_guid(value: Any, spec: FieldSpec) -> str:
    text = str(value).strip().strip("{}")
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError) as exc:
        raise ValidationError(
            f"{value!r} is not a valid GUID", field=spec.source, value=value
        ) from exc


def _to_int(value: Any, spec: FieldSpec) -> int:
    decimal = parse_decimal(value, spec)
    if decimal != decimal.to_integral_value():
        raise ValidationError(f"{value!r} is not an integer", field=spec.source, value=value)
    return int(decimal)


def parse_decimal(value: Any, spec: FieldSpec | None = None) -> Decimal:
    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        raise ValueError("empty number")
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    text = text.strip("()")
    text = text.lstrip("+-")

    comma_decimal = getattr(spec, "decimal_comma", None) if spec else None
    if comma_decimal is None:
        if "," in text and "." in text:
            comma_decimal = text.rfind(",") > text.rfind(".")
        elif "," in text:
            tail = text.rsplit(",", 1)[1]
            comma_decimal = len(tail) <= 2
        else:
            comma_decimal = False

    if comma_decimal:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")

    number = Decimal(text)
    return -number if negative else number


def _to_decimal(value: Any, spec: FieldSpec) -> Decimal:
    return parse_decimal(value, spec)


def _to_bool(value: Any, spec: FieldSpec) -> bool:
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    raise ValidationError(
        f"{value!r} is not a boolean (expected one of {sorted(_TRUE_VALUES | _FALSE_VALUES)})",
        field=spec.source,
        value=value,
    )


def parse_date(value: Any, spec: FieldSpec | None = None) -> date | datetime:
    text = str(value).strip()
    if not text:
        raise ValueError("empty date")
    formats: list[str] = []
    if spec and spec.date_format:
        formats.append(spec.date_format)
    formats.extend(DEFAULT_DATE_FORMATS)
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt in _DATE_ONLY_FORMATS:
                return parsed.date()
            return parsed
        except ValueError:
            continue
    try:
        # Handles ISO strings with timezone offsets.
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a recognised date") from exc


def _to_date(value: Any, spec: FieldSpec) -> date:
    parsed = parse_date(value, spec)
    return parsed.date() if isinstance(parsed, datetime) else parsed


def _to_datetime(value: Any, spec: FieldSpec) -> datetime:
    parsed = parse_date(value, spec)
    if isinstance(parsed, datetime):
        return parsed
    return datetime.combine(parsed, datetime.min.time())


_CONVERTERS: dict[str, Callable[[Any, FieldSpec], Any]] = {
    "string": _to_string,
    "text": _to_string,
    "guid": _to_guid,
    "int": _to_int,
    "integer": _to_int,
    "decimal": _to_decimal,
    "float": _to_decimal,
    "bool": _to_bool,
    "boolean": _to_bool,
    "date": _to_date,
    "datetime": _to_datetime,
}


def build_identity_mapping(
    columns: Sequence[str],
    *,
    entity: str | None = None,
    key_columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Generate a starter mapping document from CSV headers."""
    fields: dict[str, Any] = {}
    for column in columns:
        lowered = column.strip().lower()
        spec: dict[str, Any] = {"target": column.strip()}
        if lowered in {"id", "recordid"}:
            spec["type"] = "guid"
        elif any(token in lowered for token in ("date", "tanggal", "tgl")):
            spec["type"] = "date"
        elif any(
            token in lowered
            for token in ("amount", "price", "score", "nilai", "jumlah", "total", "nominal")
        ):
            spec["type"] = "decimal"
        else:
            spec["type"] = "string"
        fields[column] = spec

    document: dict[str, Any] = {"fields": fields}
    if entity:
        document["entity"] = entity
    if key_columns:
        document["key_fields"] = [column.strip() for column in key_columns]
    return document


__all__ = [
    "DEFAULT_DATE_FORMATS",
    "FieldSpec",
    "LookupSpec",
    "Mapper",
    "Mapping",
    "PendingLookup",
    "PreparedRow",
    "SUPPORTED_TYPES",
    "TRANSFORMS",
    "build_identity_mapping",
    "parse_date",
    "parse_decimal",
]
