"""API/settings/persistence regression checks, without requiring a webcam."""
import copy
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roadwatch.config import defaults, validate_config
from roadwatch.storage import Store
from roadwatch import app as module


@pytest.fixture
def client(tmp_path, monkeypatch):
    from roadwatch.config import ConfigStore
    from roadwatch.engine import Engine
    configuration = ConfigStore(tmp_path / "config.json")
    storage = Store(tmp_path / "db.sqlite3")
    worker = Engine(module.ROOT, configuration, module.catalog, storage)
    monkeypatch.setattr(module, "config_store", configuration)
    monkeypatch.setattr(module, "store", storage)
    monkeypatch.setattr(module, "engine", worker)
    with TestClient(module.app) as test:
        yield test


def test_config_rejects_invalid_and_unknown():
    config = defaults()
    config["calibration"]["height_m"] = -2
    with pytest.raises(ValueError):
        validate_config(config)
    with pytest.raises(ValueError):
        validate_config({"detection": {"confidence": float("nan")}})
    with pytest.raises(ValueError):
        validate_config({"detection": {"frame_stride": 1.5}})
    with pytest.raises(ValueError):
        validate_config({"pose": {"made_up": True}})


def test_upload_processing_reserves_inference_and_configuration(client, monkeypatch):
    monkeypatch.setattr(module.submission_service, "active", lambda: True)
    response = client.post("/api/session/start", json={"source_type": "dataset", "recording_id": "33"})
    assert response.status_code == 409
    assert "upload" in response.json()["detail"]
    assert client.post("/api/config", json=defaults()).status_code == 409


def test_config_and_probe(client):
    response = client.post("/api/config", json=defaults())
    assert response.status_code == 200
    assert response.json()["calibration"]["verified"] is False
    probe = client.post("/api/calibration/probe", json={"u": 360, "v": 900})
    assert probe.status_code == 200
    assert probe.json()["valid"]
    assert probe.json()["provisional"]
    assert probe.json()["latitude"] is None
    assert client.post("/api/calibration/probe", json={"u": -1, "v": 5}).status_code == 422


def test_sources_and_live_pose_validation(client):
    assert client.post("/api/session/start", json={"source_type": "dataset", "recording_id": "missing"}).status_code == 422
    assert client.post("/api/session/start", json={"source_type": "file", "source": "../outside.mp4"}).status_code == 422
    assert client.post("/api/session/start", json={"source_type": "camera", "source": "0", "pose_mode": "dataset"}).status_code == 422
    assert client.post("/api/pose", json={"latitude": 24, "longitude": 88, "heading_deg": None}).status_code == 422
    assert client.post("/api/pose", json={"latitude": 24, "longitude": 88, "heading_deg": 0, "accuracy_m": 4}).status_code == 200


def test_reference_upload_and_persistence(client):
    payload = b"lat,lon,type\n24.37570,88.62258,Pothole\n24.3,88.6,Crack\n"
    response = client.post("/api/references/import", files={"file": ("survey.csv", payload, "text/csv")})
    assert response.status_code == 200
    assert response.json()["imported_count"] == 2
    assert len(module.store.references()) == 2
    again = client.post("/api/references/import", files={"file": ("survey.csv", payload, "text/csv")})
    assert again.status_code == 200
    assert len(module.store.references()) == 2
    assert client.post("/api/references/import", files={"file": ("bad.exe", b"x")}).status_code == 422


def test_export_saved_hazard_and_formula_escape(client):
    module.store.start_session("test", "fixture", {})
    module.store.save_hazard("test", {"id": "test:1", "session_id": "test", "class_name": "=HYPERLINK(1)",
                                       "latitude": 24, "longitude": 88, "observations": 1,
                                       "match": {"reference_id": "ref1", "distance_m": 2}})
    text = client.get("/api/export.csv").text
    assert "ref1" in text
    assert "'=HYPERLINK(1)" in text
    assert client.get("/api/hazards").json()["hazards"][0]["id"] == "test:1"


def test_saved_hazard_thumbnail_still_serves_evidence(client):
    module.store.start_session("test", "fixture", {})
    module.store.save_hazard("test", {"id": "test:1", "class_name": "Pothole"})
    module.store.save_evidence("test:1", b"first jpeg", {"frame_index": 3})
    hazard = client.get("/api/hazards").json()["hazards"][0]
    response = client.get(hazard["thumbnail_url"])
    assert response.content == b"first jpeg"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/evidence/999.jpg").status_code == 404


@pytest.mark.parametrize("source_type, source_label", [("camera", "Live camera"), ("replay_detection", "Detection replay")])
def test_hazard_report_survives_detection_updates_and_includes_evidence(client, source_type, source_label):
    module.store.start_session("report", source_type, {})
    detection = {"id": "report:1", "source_type": source_type, "class_name": "Pothole", "confidence": 0.8,
                 "latitude": 24.0, "longitude": 88.0, "first_video_time_s": 1.25}
    module.store.save_hazard("report", detection)
    report_url = "/api/hazard-report?hazard_id=report%3A1"
    incident_url = "/api/incident-report?hazard_id=report%3A1"
    assert client.get(report_url).json()["severity"] == "medium"
    assert "No evidence frames saved" in client.get(incident_url).text

    details = {"title": "  Road damage  ", "address": "Main St", "notes": "<script>alert(1)</script>", "severity": "high"}
    saved = client.put(report_url, json=details)
    assert saved.status_code == 200
    assert saved.json()["title"] == "Road damage"
    assert client.put(report_url, json=details).json()["reported_at"] == saved.json()["reported_at"]
    assert client.put(report_url, json={**details, "severity": "urgent"}).status_code == 422
    assert client.put(report_url, json={**details, "latitude": 12}).status_code == 422

    module.store.save_hazard("report", {**detection, "confidence": 0.95})
    frame = module.store.save_evidence("report:1", b"jpeg", {"frame_index": 9, "time_s": 1.25})
    stored = client.get(report_url).json()
    assert stored["confidence"] == 0.95
    assert stored["severity"] == "high"
    assert stored["notes"] == details["notes"]
    incident = client.get(incident_url)
    assert incident.status_code == 200
    for text in (source_label, "Road damage", "Main St", "24.000000, 88.000000", "95%", "1.25 s", frame["url"], "&lt;script&gt;"):
        assert text in incident.text
    assert "<script>" not in incident.text


def test_reporting_unknown_hazard_returns_404(client):
    assert client.get("/api/hazard-report?hazard_id=missing").status_code == 404
    assert client.put("/api/hazard-report?hazard_id=missing", json={}).status_code == 404
    assert client.get("/api/incident-report?hazard_id=missing").status_code == 404


def test_cross_origin_control_rejected(client):
    response = client.post("/api/session/stop", headers={"Origin": "https://unrelated.invalid"})
    assert response.status_code == 403


def test_websocket_state(client):
    with client.websocket_connect("/ws") as socket:
        state = socket.receive_json()
        assert state["status"] == "idle"
        assert state["hazards"] == []
