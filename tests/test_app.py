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


def test_cross_origin_control_rejected(client):
    response = client.post("/api/session/stop", headers={"Origin": "https://unrelated.invalid"})
    assert response.status_code == 403


def test_websocket_state(client):
    with client.websocket_connect("/ws") as socket:
        state = socket.receive_json()
        assert state["status"] == "idle"
        assert state["hazards"] == []
