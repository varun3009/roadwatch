"""Reference provenance, upload limits and SQLite lifetime regressions."""
import io
import json
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

from roadwatch import app as module
from roadwatch import datasets
from roadwatch.config import ConfigStore
from roadwatch.engine import Engine
from roadwatch.storage import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    configuration = ConfigStore(tmp_path / "config.json")
    storage = Store(tmp_path / "db.sqlite3")
    worker = Engine(module.ROOT, configuration, module.catalog, storage)
    monkeypatch.setattr(module, "config_store", configuration)
    monkeypatch.setattr(module, "store", storage)
    monkeypatch.setattr(module, "engine", worker)
    with TestClient(module.app) as test:
        yield test


def workbook_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>''')
        archive.writestr("_rels/.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
        archive.writestr("xl/workbook.xml", '''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="First" sheetId="1" r:id="rId1"/><sheet name="Second" sheetId="2" r:id="rId2"/></sheets></workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>''')
        for number in (1, 2):
            archive.writestr(f"xl/worksheets/sheet{number}.xml", f'''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>id</t></is></c><c r="B1" t="inlineStr"><is><t>type</t></is></c><c r="C1" t="inlineStr"><is><t>lat</t></is></c><c r="D1" t="inlineStr"><is><t>lon</t></is></c></row>
<row r="2"><c r="A2"><v>1</v></c><c r="B2" t="inlineStr"><is><t>Pothole</t></is></c><c r="C2"><v>24.{number}</v></c><c r="D2"><v>88.1</v></c></row>
</sheetData></worksheet>''')
    return output.getvalue()


def test_changed_same_name_survey_preserves_both_explicit_row_ids(client):
    first = b"id,type,lat,lon\n1,Pothole,24.1,88.1\n"
    changed = b"id,type,lat,lon\n1,Pothole,24.2,88.2\n"
    upload = lambda data: client.post("/api/references/import", files={"file": ("survey.csv", data, "text/csv")})
    initial = upload(first)
    revised = upload(changed)
    repeated = upload(first)
    assert initial.status_code == revised.status_code == repeated.status_code == 200
    records = module.store.references()
    assert len(records) == 2
    assert len({record["id"] for record in records}) == 2
    assert {record["source_row_id"] for record in records} == {"1"}
    assert {record["latitude"] for record in records} == {24.1, 24.2}
    assert initial.json()["records"][0]["id"] == repeated.json()["records"][0]["id"]
    assert initial.json()["import_id"] != revised.json()["import_id"]
    with module.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reference_imports").fetchone()[0] == 2


def test_xlsx_sheets_can_reuse_explicit_ids_without_losing_evidence(client):
    payload = workbook_bytes()
    response = client.post("/api/references/import", files={"file": ("survey.xlsx", payload)})
    assert response.status_code == 200
    assert response.json()["imported_count"] == 2
    records = module.store.references()
    assert len({record["id"] for record in records}) == 2
    assert {record["source"] for record in records} == {"survey.xlsx#First", "survey.xlsx#Second"}
    assert {record["source_row_id"] for record in records} == {"1"}
    assert {record["source_row"] for record in records} == {2}
    assert not any("roadwatch-" in record["source"] for record in records)


@pytest.mark.parametrize("mapping", [[], {"latitude": 1}, {"sheet": True}, {"made_up": "lat"}, {"lat": "lat", "latitude": "other"}])
def test_mapping_schema_rejected_before_import(client, mapping):
    response = client.post("/api/references/import", files={"file": ("survey.csv", b"lat,lon,type\n24,88,Crack\n")}, data={"mapping": json.dumps(mapping)})
    assert response.status_code == 422
    assert module.store.references() == []


def test_workbook_expansion_is_bounded_before_xml_parse(tmp_path, monkeypatch):
    path = tmp_path / "expanded.xlsx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", "0" * 4096)
    monkeypatch.setattr(datasets, "MAX_WORKBOOK_EXPANDED_BYTES", 2048)
    result = datasets.parse_reference_file(path)
    assert result["records"] == []
    assert any("expanded-size" in issue for issue in result["issues"])


@pytest.mark.parametrize("limit,value,message", [("MAX_IMPORT_ROWS", 1, "row"), ("MAX_REFERENCE_RECORDS", 1, "event"), ("MAX_IMPORT_COLUMNS", 2, "column")])
def test_import_limits_reject_whole_file_without_partial_records(tmp_path, monkeypatch, limit, value, message):
    path = tmp_path / "survey.csv"
    path.write_text("lat,lon,type\n24,88,Crack\n25,89,Pothole\n", encoding="utf-8")
    monkeypatch.setattr(datasets, limit, value)
    result = datasets.parse_reference_file(path)
    assert result["records"] == []
    assert any(message in issue for issue in result["issues"])


def test_sqlite_connection_closes_and_rolls_back(tmp_path):
    storage = Store(tmp_path / "store.sqlite3")
    with storage.connect() as connection:
        connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        with storage.connect() as connection:
            connection.execute("INSERT INTO sessions(id) VALUES(?)", ("rolled-back",))
            raise RuntimeError("abort")
    with storage.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
