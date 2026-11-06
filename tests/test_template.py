"""Tests for the ``template`` command and its CSDL field parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from csmt.client import parse_csdl_entity_fields, parse_csdl_field_details
from csmt.errors import ConfigError, ODataError
from csmt.template import (
    build_template_columns,
    build_template_mapping,
    describe_columns,
    edm_to_mapping_type,
    sample_value,
    template_format_for,
    write_csv_template,
    write_xlsx_template,
)

CSDL = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema Namespace="Terrasoft.Configuration.OData" xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="Contact">
        <Key><PropertyRef Name="Id" /></Key>
        <Property Name="Id" Type="Edm.Guid" />
        <Property Name="Name" Type="Edm.String" Nullable="false" />
        <Property Name="Email" Type="Edm.String" MaxLength="250" />
        <Property Name="AnnualRevenue" Type="Edm.Decimal" />
        <Property Name="Confirmed" Type="Edm.Boolean" />
        <Property Name="OwnerId" Type="Edm.Guid" />
        <Property Name="CreatedOn" Type="Edm.DateTimeOffset" />
        <Property Name="CreatedById" Type="Edm.Guid" />
        <Property Name="ProcessListeners" Type="Edm.Int32" />
        <Property Name="ContactPhoto" Type="Edm.Stream" />
        <NavigationProperty Name="Owner" Type="Terrasoft.Configuration.OData.Contact" />
        <NavigationProperty Name="CreatedBy" Type="Terrasoft.Configuration.OData.Contact" />
        <NavigationProperty Name="ContactCollectionByOwner" Type="Collection(Terrasoft.Configuration.OData.Contact)" />
      </EntityType>
      <EntityContainer Name="ODataContainer">
        <EntitySet Name="Contact" EntityType="Terrasoft.Configuration.OData.Contact" />
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""


@pytest.fixture()
def details():
    return parse_csdl_field_details(CSDL, "Contact")


class TestCsdlDetails:
    def test_detects_key_nullable_maxlength_and_stream(self, details):
        by_name = {detail.name: detail for detail in details}
        assert by_name["Id"].key is True
        assert by_name["Name"].nullable is False
        assert by_name["Email"].max_length == 250
        assert by_name["Email"].nullable is True
        assert by_name["ContactPhoto"].type == "Edm.Stream"

    def test_detects_lookup_from_navigation(self, details):
        by_name = {detail.name: detail for detail in details}
        owner = by_name["OwnerId"]
        assert owner.lookup is not None
        assert owner.lookup.navigation == "Owner"
        assert owner.lookup.entity == "Contact"
        # Collection navigations must not create lookups on their own name.
        assert by_name["Id"].lookup is None

    def test_wrapper_keeps_old_shape(self, details):
        fields = parse_csdl_entity_fields(CSDL, "Contact")
        assert fields["Name"] == "Edm.String"
        assert len(fields) == len(details) == 10

    def test_unknown_entity_raises(self):
        with pytest.raises(ODataError):
            parse_csdl_field_details(CSDL, "NotThere")

    def test_html_login_page_reported_clearly(self):
        html = "<!DOCTYPE html><html><head><title>Logon</title></head></html>"
        with pytest.raises(ODataError) as excinfo:
            parse_csdl_field_details(html, "Contact")
        assert "halaman HTML" in str(excinfo.value)


class TestTemplateColumns:
    def test_drops_system_and_stream_by_default(self, details):
        headers = [column.header for column in build_template_columns(details)]
        for dropped in (
            "Id",
            "CreatedOn",
            "CreatedBy",
            "ModifiedOn",
            "ModifiedById",
            "ProcessListeners",
            "ContactPhoto",
        ):
            assert dropped not in headers
        assert headers == ["Name", "Email", "AnnualRevenue", "Confirmed", "Owner"]

    def test_include_system_keeps_them_but_not_stream(self, details):
        headers = [column.header for column in build_template_columns(details, include_system=True)]
        assert "Id" in headers
        assert "ProcessListeners" in headers
        assert "ContactPhoto" not in headers

    def test_lookup_header_keeps_odata_target(self, details):
        owner = next(c for c in build_template_columns(details) if c.header == "Owner")
        assert owner.target == "OwnerId"
        assert owner.lookup_entity == "Contact"
        assert owner.mapping == {
            "type": "lookup",
            "target": "OwnerId",
            "lookup": {"entity": "Contact", "key": "Name"},
        }

    def test_custom_lookup_key(self, details):
        owner = next(
            c for c in build_template_columns(details, lookup_key="Email") if c.header == "Owner"
        )
        assert owner.mapping["lookup"] == {"entity": "Contact", "key": "Email"}

    def test_mapping_types(self, details):
        assert edm_to_mapping_type("Edm.String") == "string"
        assert edm_to_mapping_type("Edm.Guid") == "guid"
        assert edm_to_mapping_type("Edm.Int32") == "int"
        assert edm_to_mapping_type("Edm.Decimal") == "decimal"
        assert edm_to_mapping_type("Edm.Boolean") == "bool"
        assert edm_to_mapping_type("Edm.DateTimeOffset") == "datetime"
        assert edm_to_mapping_type("Edm.Unknown") == "string"

        columns = {c.header: c for c in build_template_columns(details)}
        assert columns["AnnualRevenue"].mapping_type == "decimal"
        assert columns["Confirmed"].mapping_type == "bool"
        assert columns["Email"].mapping["max_length"] == 250

    def test_mapping_document(self, details):
        columns = build_template_columns(details)
        document = build_template_mapping(columns, entity="Contact", key_columns=["Email"])
        assert document["entity"] == "Contact"
        assert document["key_fields"] == ["Email"]
        assert document["fields"]["Owner"]["type"] == "lookup"
        assert document["fields"]["Name"]["target"] == "Name"

    def test_sample_values_and_summary(self, details):
        columns = build_template_columns(details)
        by_header = {c.header: c for c in columns}
        assert sample_value(by_header["Name"]) == "contoh teks"
        assert sample_value(by_header["Confirmed"]) == "true"
        assert sample_value(by_header["Owner"]) == "contoh Contact"
        assert describe_columns(columns) == {"total": 5, "scalar": 4, "lookup": 1}


class TestTemplateWriters:
    def test_csv_has_bom_header_and_optional_sample(self, tmp_path: Path, details):
        out = tmp_path / "contact.csv"
        columns = build_template_columns(details)
        write_csv_template(out, columns, sample=True)
        raw = out.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        lines = raw.decode("utf-8-sig").strip().split("\n")
        assert lines[0] == "Name,Email,AnnualRevenue,Confirmed,Owner"
        assert lines[1] == "contoh teks,contoh teks,1.5,true,contoh Contact"

    def test_csv_without_bom(self, tmp_path: Path, details):
        out = tmp_path / "no-bom.csv"
        write_csv_template(out, build_template_columns(details), bom=False)
        raw = out.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert len(raw.decode("utf-8").strip().split("\n")) == 1

    def test_xlsx_has_data_and_schema_sheets(self, tmp_path: Path, details):
        from openpyxl import load_workbook

        out = tmp_path / "contact.xlsx"
        columns = build_template_columns(details)
        write_xlsx_template(out, "Contact", columns, sample=True)
        assert out.exists()

        workbook = load_workbook(out)
        assert workbook.sheetnames == ["Contact", "schema"]

        data = workbook["Contact"]
        assert [cell.value for cell in data[1]] == ["Name", "Email", "AnnualRevenue", "Confirmed", "Owner"]
        assert data["E2"].value == "contoh Contact"

        schema = workbook["schema"]
        assert [cell.value for cell in schema[1]] == [
            "header",
            "target",
            "edm_type",
            "nullable",
            "max_length",
            "lookup_entity",
        ]
        owner_row = next(row for row in schema.iter_rows(min_row=2) if row[0].value == "Owner")
        assert [cell.value for cell in owner_row] == ["Owner", "OwnerId", "Edm.Guid", "yes", None, "Contact"]

    def test_format_from_extension(self, tmp_path: Path):
        assert template_format_for("out.csv") == "csv"
        assert template_format_for("out.XLSX") == "xlsx"
        with pytest.raises(ConfigError):
            template_format_for("out.json")

    def test_empty_template_refused(self, tmp_path: Path):
        with pytest.raises(ConfigError):
            write_csv_template(tmp_path / "empty.csv", [])


class TestTemplateCommand:
    def test_writes_csv_and_mapping(self, tmp_path: Path, mock):
        from csmt.cli import main

        out = tmp_path / "t.csv"
        mapping_path = tmp_path / "m.json"
        code = main(
            [
                "template",
                "--base-url",
                mock.url,
                "--username",
                "Supervisor",
                "--password",
                "secret",
                "--entity",
                "Contact",
                "--out",
                str(out),
                "--with-mapping",
                str(mapping_path),
                "--key",
                "Email",
                "--sample",
                "--quiet",
            ]
        )
        assert code == 0
        assert out.exists()
        document = json.loads(mapping_path.read_text(encoding="utf-8"))
        assert document["entity"] == "Contact"
        assert document["key_fields"] == ["Email"]
        assert document["fields"]

    def test_rejects_unknown_extension(self, tmp_path: Path, mock):
        from csmt.cli import main

        code = main(
            [
                "template",
                "--base-url",
                mock.url,
                "--username",
                "Supervisor",
                "--password",
                "secret",
                "--entity",
                "Contact",
                "--out",
                str(tmp_path / "t.json"),
                "--quiet",
            ]
        )
        assert code == 2
