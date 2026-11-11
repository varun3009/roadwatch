import csv
import hashlib
import zipfile
from pathlib import Path

import pytest

from roadwatch.datasets import DatasetCatalog, match_reference, normalize_hazard, parse_reference_file


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(rows)


@pytest.fixture
def catalog(tmp_path):
    recording = tmp_path / "Raw Data" / "34"
    (recording / "Camera").mkdir(parents=True)
    (recording / "Camera" / "1753414336425.mp4").touch()
    write_csv(recording / "1753414336425.csv", [
        "seconds_elapsed", "location_latitude", "location_longitude", "location_bearing",
        "location_horizontalAccuracy", "location_speed",
    ], [
        [0, "", "", "", "", ""],
        [1, 24, 88, 359, 3, 2],
        [2, "nan", 88, 0, 3, 2],
        [3, 24.00002, 88.00002, 1, 5, 4],
        [8, 24.0001, 88.0001, -1, -1, -1],
        [9, 91, 88, 4, 3, 2],
        [10, "inf", 88, 4, 3, 2],
    ])
    write_csv(recording / "Annotation.csv", ["seconds_elapsed", "text"], [
        [2, "Pothole"], [2, "Potholes"], [6, "Bump"], ["nan", "Crack"],
    ])
    return DatasetCatalog(tmp_path)


def test_catalog_discovers_sensor_file_by_header_and_rejects_nonfinite_gps(catalog):
    recordings = catalog.recordings()
    assert len(recordings) == 1
    assert Path(recordings[0]["sensor_path"]).name == "1753414336425.csv"
    assert Path(recordings[0]["video_path"]).suffix == ".mp4"
    assert [row["time_s"] for row in catalog.gps_track("34")] == [1, 3, 8]
    assert catalog.gps_track("34")[-1]["heading_deg"] is None
    assert catalog.gps_track("34")[-1]["accuracy_m"] is None
    assert catalog.gps_track("34")[-1]["speed_mps"] is None
    assert catalog.gps_track("../34") == []


def test_pose_interpolates_heading_circularly_and_refuses_gap_or_extrapolation(catalog):
    pose = catalog.pose_at("34", 2)
    assert pose["latitude"] == pytest.approx(24.00001)
    assert pose["longitude"] == pytest.approx(88.00001)
    assert pose["heading_deg"] == pytest.approx(0)
    assert pose["accuracy_m"] == pytest.approx(4)
    assert pose["speed_mps"] == pytest.approx(3)
    assert catalog.pose_at("34", 0) is None
    assert catalog.pose_at("34", 9) is None
    assert catalog.pose_at("34", 6) is None
    assert catalog.pose_at("34", float("nan")) is None
    assert catalog.pose_at("34", 2, max_gap_s=1) is None
    assert catalog.pose_at("34", 8)["time_s"] == 8


def test_longitude_interpolation_uses_short_path_at_date_line(tmp_path):
    directory = tmp_path / "Raw Data" / "1"
    write_csv(directory / "1.csv", ["seconds_elapsed", "location_latitude", "location_longitude"], [
        [0, 10, 179.9], [2, 10, -179.9],
    ])
    pose = DatasetCatalog(tmp_path).pose_at("1", 1)
    assert abs(pose["longitude"]) == pytest.approx(180)
    assert pose["heading_deg"] is None


def test_raw_references_deduplicate_events_and_label_vehicle_coordinates(catalog):
    events = catalog.reference_events("34")
    assert len(events) == 2
    assert events[0]["hazard_type"] == "pothole"
    assert events[0]["location_kind"] == "vehicle_at_annotation"
    assert events[1]["location_kind"] == "unavailable"
    assert events[1]["latitude"] is None
    assert catalog.reference_events("missing") == []
    summary = catalog.summary()
    assert summary["reference_event_count"] == 2
    assert summary["workbook_count"] == 0
    assert any("No Excel workbook" in issue for issue in summary["issues"])


def test_csv_import_maps_columns_deduplicates_and_preserves_source(tmp_path):
    path = tmp_path / "survey.csv"
    write_csv(path, ["Defect", "GPS North", "GPS East", "Clip", "Elapsed"], [
        ["Potholes", 24, 88, 34, 4],
        ["Pothole", 24, 88, 34, 4],
        ["Cracks", "nan", "inf", "", ""],
        ["Bump", "", "", 34, 8],
    ])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = parse_reference_file(path, {
        "type": "Defect", "lat": "GPS North", "lon": "GPS East", "recording": "Clip", "time": "Elapsed",
    })
    assert len(result["records"]) == 2
    assert result["records"][0]["hazard_type"] == "pothole"
    assert result["records"][0]["recording_id"] == "34"
    assert result["records"][1]["location_kind"] == "unavailable"
    assert result["issues"] and "skipped" in result["issues"][0]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_windowed_csv_reduces_sensor_samples_to_events(tmp_path):
    path = tmp_path / "34" / "isolated_all_anomalies.csv"
    write_csv(path, ["seconds_elapsed", "annotation_text", "location_latitude", "location_longitude", "annotation_millisecond_press_duration"], [
        [4, "Pothole", 24, 88, 96], [4.01, "Pothole", 24, 88, ""],
        [4.02, "Pothole", 24, 88, ""], [11.9, "Pothole", 24, 88.0001, 82],
        [11.91, "Pothole", 24, 88.0001, ""],
    ])
    result = parse_reference_file(path)
    assert [record["time_s"] for record in result["records"]] == [4, 11.9]
    assert all(record["recording_id"] == "34" for record in result["records"])
    assert all(record["location_kind"] == "vehicle_at_annotation" for record in result["records"])


def test_window_without_markers_collapses_contiguous_runs(tmp_path):
    path = tmp_path / "34" / "isolated.csv"
    write_csv(path, ["seconds_elapsed", "annotation_text", "location_latitude", "location_longitude"], [
        [4, "Pothole", 24, 88], [4.01, "Pothole", 24, 88],
        [4.02, "Pothole", 24, 88], [8, "Pothole", 24, 88.0001],
    ])
    result = parse_reference_file(path)
    assert [record["time_s"] for record in result["records"]] == [4, 8]


def test_workbook_import_selects_sheet_and_reads_cached_values_without_changes(tmp_path):
    pytest.importorskip("openpyxl")
    path = tmp_path / "references.xlsx"
    # A small immutable-format fixture built with stdlib ZIP/XML. Production
    # code only reads workbooks; it never creates or edits the user's files.
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '''<?xml version="1.0"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>''')
        archive.writestr("_rels/.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
        archive.writestr("xl/workbook.xml", '''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Notes" sheetId="1" r:id="rId1"/><sheet name="Survey" sheetId="2" r:id="rId2"/></sheets></workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>''')
        archive.writestr("xl/worksheets/sheet1.xml", '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>description</t></is></c></row></sheetData></worksheet>''')
        archive.writestr("xl/worksheets/sheet2.xml", '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>Defect</t></is></c><c r="B1" t="inlineStr"><is><t>North</t></is></c><c r="C1" t="inlineStr"><is><t>East</t></is></c><c r="D1" t="inlineStr"><is><t>Trip</t></is></c><c r="E1" t="inlineStr"><is><t>Moment</t></is></c></row>
<row r="2"><c r="A2" t="inlineStr"><is><t>Cracks</t></is></c><c r="B2"><v>24.1</v></c><c r="C2"><v>88.2</v></c><c r="D2"><v>34</v></c><c r="E2"><v>2.5</v></c></row>
<row r="3"><c r="A3" t="inlineStr"><is><t>Crack</t></is></c><c r="B3"><f>1+1</f></c><c r="C3"><v>88.2</v></c></row>
</sheetData></worksheet>''')
    before = path.read_bytes()
    parsed = parse_reference_file(path, {
        "sheet": "Survey", "hazard_type": "Defect", "latitude": "North", "longitude": "East",
        "recording_id": "Trip", "time_s": "Moment",
    })
    assert parsed["sheets"] == ["Survey"]
    assert parsed["columns"] == ["Defect", "North", "East", "Trip", "Moment"]
    assert len(parsed["records"]) == 1
    assert parsed["records"][0]["hazard_type"] == "crack"
    assert parsed["records"][0]["recording_id"] == "34"
    assert any("skipped" in issue for issue in parsed["issues"])
    assert path.read_bytes() == before
    assert "does not exist" in parse_reference_file(path, {"sheet": "Missing"})["issues"][0]


def test_import_errors_are_recoverable(tmp_path):
    assert "Unsupported" in parse_reference_file(tmp_path / "old.xls")["issues"][0]
    path = tmp_path / "bad.xlsx"
    path.write_text("not a workbook")
    assert parse_reference_file(path)["records"] == []
    assert parse_reference_file(path)["issues"]


def reference(**changes):
    base = {"id": "ref-1", "recording_id": "34", "time_s": 4, "hazard_type": "pothole", "latitude": 24, "longitude": 88}
    return {**base, **changes}


def test_match_requires_type_recording_time_and_distance_agreement():
    event = reference(id="event", hazard_type="Potholes", time_s=4.2, latitude=24.00001)
    match = match_reference(event, [reference()])
    assert match["reference_id"] == "ref-1"
    assert 1 < match["distance_m"] < 2
    assert match["time_delta_s"] == pytest.approx(0.2)
    assert match["match_kind"] == "spatial_and_temporal"
    assert match_reference(event, [reference(hazard_type="crack")]) is None
    assert match_reference(event, [reference(recording_id="35")]) is None
    assert match_reference(event, [reference(time_s=40)]) is None
    assert match_reference(event, [reference(latitude=24.1)]) is None
    assert match_reference(event, [reference(recording_id=None)]) is None
    assert match_reference(event, [reference(recording_id=None)], same_recording_only=False)
    assert match_reference(event, [reference(recording_id="35")], same_recording_only=False) is None


def test_temporal_matches_require_recording_and_are_distinguished_from_spatial():
    event = reference(latitude=None, longitude=None)
    match = match_reference(event, [reference()])
    assert match["match_kind"] == "temporal"
    assert match["distance_m"] is None
    assert match_reference({**event, "recording_id": None}, [reference()], same_recording_only=False) is None
    assert match_reference({**event, "time_s": None}, [reference()]) is None
    assert normalize_hazard("Speed Bumps") == "bump"
    assert all(normalize_hazard(label) == "crack" for label in ["Longitudinal", "Transverse", "Alligator", "Longitudinal crack"])


def test_supplied_recording_34_has_two_events_not_791_sensor_rows():
    root = Path(__file__).resolve().parents[1]
    if not (root / "Raw Data" / "34" / "Annotation.csv").exists():
        pytest.skip("Supplied recording 34 is not available")
    catalog = DatasetCatalog(root)
    events = catalog.reference_events("34")
    assert len(events) == 2
    assert [event["hazard_type"] for event in events] == ["pothole", "pothole"]
    assert events[0]["time_s"] == pytest.approx(4.002)
    assert events[0]["latitude"] == pytest.approx(24.3757020686)
    assert events[0]["longitude"] == pytest.approx(88.6225815690)
    assert events[1]["time_s"] == pytest.approx(11.901000244140626)
    assert events[0]["location_kind"] == "vehicle_at_annotation"
    isolated = root / "Isolated Data" / "Road Anomalies" / "Road Anomalies (1-86)" / "34" / "isolated_all_anomalies.csv"
    if isolated.exists():
        assert len(parse_reference_file(isolated)["records"]) == 2
