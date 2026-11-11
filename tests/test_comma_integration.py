"""Comma replay/API integration with isolated data and analytic camera poses."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from roadwatch import app as module
from roadwatch.catalog import UnifiedCatalog
from roadwatch.config import ConfigStore
from roadwatch.engine import Engine
from roadwatch.storage import Store


def save_array(directory, name, values):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("wb") as stream:
        np.save(stream, np.asarray(values))


@pytest.fixture
def integration(tmp_path, monkeypatch):
    # The two datasets intentionally overlap in position/time: filtering should
    # depend on recording identity, not the continents happening to differ.
    roadsens = tmp_path / "Raw Data" / "33"
    (roadsens / "Camera").mkdir(parents=True)
    (roadsens / "Camera" / "video.mp4").write_bytes(b"fixture video")
    (roadsens / "33.csv").write_text(
        "seconds_elapsed,location_latitude,location_longitude,location_bearing,location_speed,location_horizontalAccuracy\n"
        "0,0,0,0,10,3\n1,0,0,0,10,3\n", encoding="utf-8")
    (roadsens / "Annotation.csv").write_text("seconds_elapsed,text\n0,Pothole\n", encoding="utf-8")
    segment = tmp_path / "datasets" / "comma2k19" / "Example_1" / "route_2018-08-02--08-34-47" / "40"
    segment.mkdir(parents=True)
    (segment / "video.hevc").write_bytes(b"fixture video")
    pose = segment / "global_pose"
    save_array(pose, "frame_times", [8000, 8000.007, 8000.015])
    save_array(pose, "frame_positions", np.tile([6_378_139.2, 0, 0], (3, 1)))
    save_array(pose, "frame_orientations", np.tile([math.sqrt(.5), 0, -math.sqrt(.5), 0], (3, 1)))
    save_array(pose, "frame_velocities", np.tile([0, 0, 10], (3, 1)))
    (tmp_path / "best.pt").write_bytes(b"model fixture")
    configuration = ConfigStore(tmp_path / "runtime" / "config.json")
    configured = copy.deepcopy(configuration.value)
    configured["calibration"].update(fx=777, fy=778, cx=330, cy=630,
                                      pitch_deg=25, yaw_deg=12, roll_deg=4,
                                      offset_forward_m=3, offset_right_m=1,
                                      height_m=2.2, verified=True)
    configured["sync_offset_s"] = 37
    configured["detection"]["frame_stride"] = 1
    configuration.save(configured)
    catalog = UnifiedCatalog(tmp_path)
    storage = Store(tmp_path / "runtime" / "db.sqlite3")
    storage.save_references("roadsens survey", [{"id": "same-position", "recording_id": "33",
                            "hazard_type": "pothole", "time_s": 0, "latitude": 0, "longitude": 0}])
    worker = Engine(tmp_path, configuration, catalog, storage)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "config_store", configuration)
    monkeypatch.setattr(module, "catalog", catalog)
    monkeypatch.setattr(module, "store", storage)
    monkeypatch.setattr(module, "engine", worker)
    monkeypatch.setattr(module, "_download_catalog_marker", None)
    comma = next(record for record in catalog.recordings() if record["dataset_id"] == "comma2k19")
    with TestClient(module.app) as client:
        yield SimpleNamespace(client=client, worker=worker, catalog=catalog, storage=storage,
                              config=configuration, comma=comma, segment=segment, root=tmp_path)


class _Tensor:
    def __init__(self, values):
        self.values = values

    def cpu(self):
        return self

    def tolist(self):
        return self.values


def fake_inference(monkeypatch):
    class VideoCapture:
        def __init__(self):
            self.index = -1

        def open(self, *args):
            pass

        def isOpened(self):
            return True

        def set(self, *args):
            return True

        def get(self, prop):
            # Both values deliberately disagree with the exact frame clock.
            import cv2
            return 1000 if prop == cv2.CAP_PROP_FPS else 99_999

        def grab(self):
            self.index += 1
            return self.index < 3

        def retrieve(self):
            return True, np.zeros((874, 1164, 3), dtype=np.uint8)

        def release(self):
            pass

    class YOLO:
        names = {0: "pothole"}

        def __init__(self, path):
            pass

        def track(self, frame, **kwargs):
            return [SimpleNamespace(boxes=SimpleNamespace(
                id=_Tensor([7]), xyxy=_Tensor([[580.0, 580.0, 584.0, 700.0]]),
                cls=_Tensor([0]), conf=_Tensor([0.9]),
            ))]

    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=YOLO))
    monkeypatch.setattr("roadwatch.engine.cv2.VideoCapture", VideoCapture)


def run_session(integration):
    response = integration.client.post("/api/session/start", json={
        "source_type": "dataset", "recording_id": integration.comma["id"], "pose_mode": "dataset"})
    assert response.status_code == 200
    integration.worker.thread.join(timeout=5)
    assert not integration.worker.thread.is_alive()
    final = integration.client.get("/api/state").json()
    assert final["status"] == "completed", final
    return response.json(), final


def test_selecting_comma_uses_published_intrinsics_without_overwriting_roadsens(integration):
    old = copy.deepcopy(integration.config.value)
    selected = integration.client.get("/api/recordings/" + integration.comma["id"]).json()
    assert selected["recording"]["dataset_id"] == "comma2k19"
    assert selected["has_labels"] is False
    assert selected["references"] == []
    assert selected["calibration"]["fx"] == 910
    assert selected["calibration"]["width"] == 1164
    assert selected["calibration"]["height_m"] == 2.2
    assert selected["calibration"]["pitch_deg"] == 0
    assert selected["calibration"]["verified"] is False
    assert integration.config.value == old
    assert json.loads(integration.config.path.read_text())["calibration"] == old["calibration"]
    roadsens = integration.client.get("/api/recordings/33").json()
    assert roadsens["calibration"] == old["calibration"]
    assert len(roadsens["references"]) == 2


def test_combined_summary_and_api_datasets_preserve_reference_counts(integration):
    bootstrap = integration.client.get("/api/bootstrap").json()
    assert bootstrap["dataset"]["recording_count"] == 2
    assert bootstrap["dataset"]["playable_recording_count"] == 2
    assert bootstrap["dataset"]["sensor_recording_count"] == 2
    assert bootstrap["dataset"]["reference_event_count"] == 1
    assert bootstrap["dataset"]["unlabeled_recording_count"] == 1
    datasets = {item["id"]: item for item in bootstrap["datasets"]}
    assert datasets["comma2k19"]["reference_event_count"] == 0
    assert datasets["comma2k19"]["has_labels"] is False
    assert datasets["roadsens"]["reference_event_count"] == 1
    assert integration.catalog.reference_events(integration.comma["id"]) == []
    assert len(integration.catalog.reference_events()) == 1
    assert integration.client.get("/api/datasets").status_code == 200


def test_replay_uses_exact_frame_times_full_camera_quaternion_and_no_roadsens_labels(integration, monkeypatch):
    fake_inference(monkeypatch)
    old = copy.deepcopy(integration.config.value)
    initial, final = run_session(integration)
    assert initial["active_calibration"]["fx"] == 910
    assert final["frame_index"] == 2
    assert final["video_time_s"] == pytest.approx(0.015)
    assert final["sensor_time_s"] == pytest.approx(0.015)
    assert final["dataset_id"] == "comma2k19"
    assert final["has_reference_labels"] is False
    assert final["pose"]["orientation_convention"] == "camera_frd_to_ecef_hamilton_wxyz"
    assert final["pose"]["position_reference"] == "camera"
    detection = final["detections"][0]
    # An independent analytic horizontal north-facing ray at equator. Applying
    # the configured manual pitch/yaw/lever arm would violate these signs/range.
    assert detection["distance_m"] == pytest.approx(2.2 * 910 / (700 - 437), rel=1e-7)
    assert detection["latitude"] > 0
    assert detection["longitude"] == pytest.approx(0, abs=1e-9)
    assert detection["method"] == "comma2k19_fused_pose_flat_road"
    assert detection["distance_kind"] == "horizontal_from_camera"
    assert detection["gps_accuracy_m"] is None
    assert detection["match"] is None
    saved = integration.storage.hazards(final["session_id"])
    assert len(saved) == 1
    assert saved[0]["observations"] == 3
    assert saved[0]["has_reference_labels"] is False
    assert saved[0]["dataset_id"] == "comma2k19"
    assert saved[0]["method"] == "comma2k19_fused_pose_flat_road"
    assert saved[0]["match"] is None
    assert integration.config.value == old
    # Persisted session settings use native sync without changing saved settings.
    with integration.storage.connect() as db:
        session = json.loads(db.execute("SELECT config FROM sessions WHERE id=?", (final["session_id"],)).fetchone()[0])
    assert session["sync_offset_s"] == 0
    assert old["sync_offset_s"] == 37
    probe = integration.client.post("/api/calibration/probe", json={"u": 582, "v": 700, "frame_width": 1164, "frame_height": 874}).json()
    assert probe["valid"] is True
    assert probe["distance_m"] == pytest.approx(detection["distance_m"])


def test_gnss_fallback_shows_track_without_manufacturing_camera_range_or_hazard_gps(integration, monkeypatch):
    integration.catalog.comma.invalidate()  # Release native Windows array maps.
    (integration.segment / "global_pose" / "frame_orientations").unlink()
    gnss = integration.segment / "processed_log" / "GNSS" / "live_gnss_ublox"
    save_array(gnss, "t", [7999.9, 8000, 8000.02, 8000.1])
    save_array(gnss, "value", np.tile([0, 0, 10, 0, 2.2, 0], (4, 1)))
    integration.catalog.refresh()
    fake_inference(monkeypatch)
    _, final = run_session(integration)
    assert final["pose"]["pose_mode"] == "gnss_fallback"
    assert "ecef_orientation" not in final["pose"]
    detection = final["detections"][0]
    assert detection["distance_m"] is None
    assert detection["latitude"] is None
    assert detection["longitude"] is None
    assert detection["projection_reason"]
    assert final["pose"]["latitude"] == 0
    saved = integration.storage.hazards(final["session_id"])[0]
    assert saved["located_observations"] == 0
    assert saved["latitude"] is None
    probe = integration.client.post("/api/calibration/probe", json={"u": 582, "v": 700, "frame_width": 1164, "frame_height": 874}).json()
    assert probe["valid"] is False
    assert probe["distance_m"] is None
    assert probe["latitude"] is None
