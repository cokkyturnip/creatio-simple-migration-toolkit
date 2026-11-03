"""Unit tests for mapping, type coercion and lookup preparation."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from csmt.client import json_default
from csmt.csv_source import Row
from csmt.errors import MappingError, ValidationError
from csmt.mapping import (
    Mapper,
    Mapping,
    build_identity_mapping,
    parse_date,
    parse_decimal,
)


def make_row(**values: str) -> Row:
    return Row(lineno=2, values=values)


def make_mapper(fields: dict, **top_level) -> Mapper:
    document = {"fields": fields, **top_level}
    return Mapper(Mapping.from_dict(document))


def test_identity_mapping_maps_all_columns() -> None:
    mapping = Mapping.identity(["Name", "Email", "Id"], key_fields=["Email"], entity="Contact")
    assert mapping.targets == ["Name", "Email", "Id"]
    assert mapping.key_fields == ["Email"]
    assert mapping.entity == "Contact"
    assert mapping.fields[2].type == "guid"


def test_mapping_reports_missing_sources() -> None:
    mapping = Mapping.identity(["Name", "Email"])
    assert mapping.missing_sources(["Name", "Email", "Phone"]) == []
    assert mapping.missing_sources(["Name"]) == ["Email"]


def test_transforms_and_defaults() -> None:
    mapper = make_mapper(
        {
            "nama": {"target": "Name", "transform": "title"},
            "email": {"target": "Email", "transform": "lower"},
            "phone": {"target": "Phone", "transform": "digits"},
            "notes": {"target": "Description", "default": "imported"},
            "middle": {"target": "MiddleName", "null_for_empty": True},
        },
        keys=["Email"],
    )
    prepared = mapper.prepare(
        make_row(
            nama="  john   doe ",
            email=" John.Doe@Example.COM ",
            phone="+62 (812) 345",
            notes="",
            middle="",
        )
    )
    assert prepared.payload["Name"] == "John Doe"
    assert prepared.payload["Email"] == "john.doe@example.com"
    assert prepared.payload["Phone"] == "62812345"
    assert prepared.payload["Description"] == "imported"
    assert prepared.payload["MiddleName"] is None
    assert prepared.key_values == {"Email": "john.doe@example.com"}


def test_required_empty_value_fails() -> None:
    mapper = make_mapper({"Name": {"target": "Name", "required": True}})
    with pytest.raises(ValidationError) as excinfo:
        mapper.prepare(make_row(Name="   "))
    assert "Name" in str(excinfo.value)


def test_constants_and_mapping_defaults() -> None:
    mapper = make_mapper(
        {"Name": {"target": "Name"}},
        constants={"ProcessListeners": 0},
        defaults={"Active": True},
    )
    prepared = mapper.prepare(make_row(Name="Alice"))
    assert prepared.payload == {"ProcessListeners": 0, "Name": "Alice", "Active": True}


def test_decimal_with_indonesian_separators() -> None:
    mapper = make_mapper({"Score": {"target": "UsrScore", "type": "decimal"}})
    prepared = mapper.prepare(make_row(Score="1.234,56"))
    assert prepared.payload["UsrScore"] == Decimal("1234.56")
    assert json.dumps(prepared.payload, default=json_default) == '{"UsrScore": "1234.56"}'


def test_decimal_us_format() -> None:
    assert parse_decimal("1,234.56") == Decimal("1234.56")
    assert parse_decimal("(1,234.56)") == Decimal("-1234.56")
    assert parse_decimal("42") == Decimal("42")


def test_int_rejects_fraction() -> None:
    mapper = make_mapper({"Count": {"target": "ProcessListeners", "type": "int"}})
    with pytest.raises(ValidationError):
        mapper.prepare(make_row(Count="1.5"))
    assert mapper.prepare(make_row(Count="1,234")).payload["ProcessListeners"] == 1234


@pytest.mark.parametrize(
    "raw,expected", [("Ya", True), ("tidak", False), ("1", True), ("0", False)]
)
def test_boolean_conversion(raw: str, expected: bool) -> None:
    mapper = make_mapper({"Active": {"target": "Active", "type": "bool"}})
    assert mapper.prepare(make_row(Active=raw)).payload["Active"] is expected


def test_boolean_invalid() -> None:
    mapper = make_mapper({"Active": {"target": "Active", "type": "bool"}})
    with pytest.raises(ValidationError):
        mapper.prepare(make_row(Active="mungkin"))


def test_date_formats() -> None:
    mapper = make_mapper({"Birth": {"target": "BirthDate", "type": "date"}})
    assert mapper.prepare(make_row(Birth="31/12/2024")).payload["BirthDate"] == date(2024, 12, 31)
    assert mapper.prepare(make_row(Birth="2024-12-31")).payload["BirthDate"] == date(2024, 12, 31)

    explicit = make_mapper(
        {"Birth": {"target": "BirthDate", "type": "date", "format": "%d-%b-%Y"}}
    )
    assert explicit.prepare(make_row(Birth="05-Jan-2024")).payload["BirthDate"] == date(2024, 1, 5)

    with pytest.raises(ValidationError):
        mapper.prepare(make_row(Birth="not a date"))


def test_datetime_conversion() -> None:
    mapper = make_mapper({"Created": {"target": "CreatedOn", "type": "datetime"}})
    value = mapper.prepare(make_row(Created="2024-01-31 08:15:00")).payload["CreatedOn"]
    assert value == datetime(2024, 1, 31, 8, 15)
    assert json_default(value) == "2024-01-31T08:15:00"


def test_parse_date_iso_with_offset() -> None:
    parsed = parse_date("2024-01-31T08:15:00Z")
    assert isinstance(parsed, datetime)
    assert parsed.year == 2024


def test_guid_conversion() -> None:
    mapper = make_mapper({"Id": {"target": "Id", "type": "guid"}})
    prepared = mapper.prepare(make_row(Id="{62F9BC01-57CF-4CC7-90BF-8672ACC922E2}"))
    assert prepared.payload["Id"] == "62f9bc01-57cf-4cc7-90bf-8672acc922e2"

    with pytest.raises(ValidationError):
        mapper.prepare(make_row(Id="not-a-guid"))


def test_choices_mapping() -> None:
    mapper = make_mapper(
        {"Gender": {"target": "Gender", "choices": {"L": "Male", "P": "Female"}}}
    )
    assert mapper.prepare(make_row(Gender=" L ")).payload["Gender"] == "Male"
    with pytest.raises(ValidationError):
        mapper.prepare(make_row(Gender="X"))


def test_max_length() -> None:
    mapper = make_mapper({"Code": {"target": "Code", "max_length": 3}})
    with pytest.raises(ValidationError):
        mapper.prepare(make_row(Code="ABCD"))


def test_lookup_pending_and_finalize() -> None:
    mapper = make_mapper(
        {
            "Negara": {
                "target": "CountryId",
                "type": "lookup",
                "lookup": {"entity": "Country", "key_field": "Name"},
            },
            "Email": {"target": "Email"},
        },
        keys=["Email"],
    )
    prepared = mapper.prepare(make_row(Negara="Indonesia", Email="a@x"))
    assert prepared.payload["CountryId"] is None
    assert len(prepared.pending) == 1
    assert prepared.pending[0].entity == "Country"
    assert prepared.pending[0].value == "Indonesia"

    prepared.pending[0].resolved_id = "11111111-1111-1111-1111-111111111111"
    mapper.finalize(prepared)
    assert prepared.payload["CountryId"] == "11111111-1111-1111-1111-111111111111"
    assert prepared.pending == []


def test_lookup_missing_id_fails_on_finalize() -> None:
    mapper = make_mapper(
        {
            "Negara": {
                "target": "CountryId",
                "type": "lookup",
                "lookup": {"entity": "Country", "key_field": "Name"},
            }
        }
    )
    prepared = mapper.prepare(make_row(Negara="Atlantis"))
    with pytest.raises(ValidationError) as excinfo:
        mapper.finalize(prepared)
    assert "Atlantis" in str(excinfo.value)


def test_lookup_requires_spec() -> None:
    with pytest.raises(MappingError):
        make_mapper({"Negara": {"target": "CountryId", "type": "lookup"}})


def test_duplicate_targets_rejected() -> None:
    with pytest.raises(MappingError):
        Mapping.from_dict(
            {
                "fields": {
                    "A": {"target": "Name"},
                    "B": {"target": "Name"},
                }
            }
        )


def test_key_must_be_mapped() -> None:
    with pytest.raises(MappingError):
        Mapping.from_dict({"fields": {"A": {"target": "Name"}}, "key_fields": ["Email"]})


def test_unknown_type_and_transform_rejected() -> None:
    with pytest.raises(MappingError):
        Mapping.from_dict({"fields": {"A": {"target": "Name", "type": "blob"}}})
    with pytest.raises(MappingError):
        Mapping.from_dict({"fields": {"A": {"target": "Name", "transform": "shout"}}})


def test_mapping_load_from_file(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text(
        json.dumps({"entity": "Contact", "key_fields": ["Email"], "fields": {"Email": "Email"}}),
        encoding="utf-8",
    )
    mapping = Mapping.load(path)
    assert mapping.entity == "Contact"
    assert mapping.key_fields == ["Email"]


def test_mapping_load_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(MappingError):
        Mapping.load(path)


def test_validate_targets() -> None:
    mapping = Mapping.identity(["Name", "Email"])
    mapping.validate_targets(["Id", "Name", "Email"])
    with pytest.raises(MappingError) as excinfo:
        mapping.validate_targets(["Id", "Name"])
    assert "Email" in str(excinfo.value)


def test_build_identity_mapping_heuristics() -> None:
    document = build_identity_mapping(
        ["Id", "Tanggal Lahir", "Total Amount", "Name"], entity="Contact", key_columns=["Id"]
    )
    assert document["fields"]["Id"]["type"] == "guid"
    assert document["fields"]["Tanggal Lahir"]["type"] == "date"
    assert document["fields"]["Total Amount"]["type"] == "decimal"
    assert document["fields"]["Name"]["type"] == "string"
    assert document["entity"] == "Contact"
    assert document["key_fields"] == ["Id"]


def test_missing_key_value_fails_at_prepare() -> None:
    mapper = make_mapper({"Email": {"target": "Email"}}, keys=["Email"])
    with pytest.raises(ValidationError):
        mapper.prepare(make_row(Email=""))
