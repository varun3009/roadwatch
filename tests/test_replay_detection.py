"""Annotated replay is persistent and idempotent even after scrubbing/restart."""
import base64
import copy
import threading
import time
from types import SimpleNamespace

import cv2
from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np
import pytest

from roadwatch.config import ConfigStore
from roadwatch.engine import Engine, HazardAccumulator
from roadwatch.replay import ReplayService
from roadwatch.replay_detection import ReplayDetectionService, make_replay_detection_router
from roadwatch.storage import Store
from roadwatch.submissions import SubmissionService


class Tensor:
    def __init__(self, values): self.values = values
    def cpu(self): return self
    def tolist(self): return self.values


class Boxes(SimpleNamespace):
    def __len__(self): return len(self.xyxy.values)


class Model:
    names = {0: "Pothole", 1: "Longitudinal"}
    def __init__(self): self.calls = 0; self.empty = False
    def predict(self, frame, **kwargs):
        self.calls += 1
        boxes = Boxes(xyxy=Tensor([[10., 10., 50., 45.], [65., 15., 105., 55.]]), cls=Tensor([0., 1.]), conf=Tensor([.9, .8]), id=None)
        return [SimpleNamespace(boxes=None if self.empty else boxes)]


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "road.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (120, 80))
    assert writer.isOpened()
    for index in range(100): writer.write(np.full((80, 120, 3), 60 + index, np.uint8))
    writer.release()
    (tmp_path / "best.pt").write_bytes(b"test-model")
    config = ConfigStore(tmp_path / "config.json")
    config.value["calibration"].update(width=120, height=80, fx=100, fy=100, cx=60, cy=40)
    record = {"id": "1", "video_path": str(path), "label": "Test road", "dataset_id": "roadsens", "has_labels": False}
    catalog = SimpleNamespace(recording=lambda identity: record if str(identity) == "1" else None,
                              recordings=lambda: [record], reference_events=lambda identity: [],
                              pose_at=lambda *args, **kwargs: {"latitude": 24.3, "longitude": 88.6, "heading_deg": 90, "accuracy_m": 3})
    store = Store(tmp_path / "reports.sqlite3")
    engine = Engine(tmp_path, config, catalog, store)
    gate = SubmissionService(tmp_path, store, config, engine)
    replay = ReplayService(tmp_path, catalog, store)
    service = ReplayDetectionService(tmp_path, catalog, store, config, engine, gate, replay)
    model = Model()
    service._load_model = lambda context: model
    app = FastAPI()
    app.include_router(make_replay_detection_router(service))
    with TestClient(app) as client:
        yield SimpleNamespace(root=tmp_path, path=path, config=config, catalog=catalog, store=store, engine=engine,
                              gate=gate, replay=replay, service=service, model=model, client=client)
    service.stop(); gate.stop(); replay.close()


def analyze(setup, source="dataset:1"):
    response = setup.client.post("/api/replay/analyze", json={"source_id": source})
    assert response.status_code == 200, response.text
    return response.json()


def process(setup, analysis, timestamp):
    response = setup.client.post("/api/replay/process", json={"analysis_id": analysis["analysis_id"], "time_s": timestamp})
    assert response.status_code == 200, response.text
    return response.json()


def test_replay_returns_large_annotations_and_saves_hazards(setup):
    analysis = analyze(setup)
    assert analysis["info"]["detection_enabled"] is True
    result = process(setup, analysis, .2)
    assert result["cached"] is False
    assert setup.model.calls == 1
    assert len(result["detections"]) == len(result["hazards"]) == 2
    assert result["evidence_count"] == 2
    frame = cv2.imdecode(np.frombuffer(base64.b64decode(result["image_url"].split(",", 1)[1]), np.uint8), cv2.IMREAD_COLOR)
    # Original is gray. Boxes/labels introduce visible colored pixels.
    assert np.max(frame[:, :, 2].astype(int) - frame[:, :, 0].astype(int)) > 100
    for hazard in result["hazards"]:
        assert hazard["thumbnail_url"]
        assert hazard["evidence_count"] == 1
        assert hazard["latitude"] is not None
        assert hazard["first_video_time_s"] == .2
        assert hazard["source_type"] == "replay_detection"


def test_repeated_cached_frame_does_not_infer_or_increment_any_observation(setup):
    analysis = analyze(setup)
    first = process(setup, analysis, .25)
    repeated = process(setup, analysis, .29)
    assert first["frame_index"] == repeated["frame_index"] == 2
    assert repeated["cached"] is True
    assert setup.model.calls == 1
    assert first["image_url"] == repeated["image_url"]
    assert all(hazard["observations"] == 1 and hazard["evidence_count"] == 1 for hazard in repeated["hazards"])


def test_backward_seek_uses_neighbor_identity_and_preserves_video_time_bounds(setup):
    setup.config.value["sync_offset_s"] = 37
    analysis = analyze(setup)
    process(setup, analysis, .8)
    process(setup, analysis, .4)
    result = process(setup, analysis, .6)
    assert len(result["hazards"]) == 2
    assert all(hazard["observations"] == 3 for hazard in result["hazards"])
    assert all(hazard["first_seen_s"] == 37.4 and hazard["last_seen_s"] == 37.8 for hazard in result["hazards"])
    assert all(hazard["first_video_time_s"] == .4 and hazard["last_video_time_s"] == .8 for hazard in result["hazards"])


def test_ten_frame_cap_and_nearby_frame_association_remain_stable(setup):
    analysis = analyze(setup)
    for index in range(15):
        result = process(setup, analysis, index * .6)
    assert len(result["hazards"]) == 2
    assert all(hazard["observations"] == 15 and hazard["evidence_count"] == 10 for hazard in result["hazards"])
    assert result["evidence_count"] == 20


def test_source_alias_reload_and_restart_reuse_cached_analysis_and_hazards(setup):
    analysis = analyze(setup)
    first = process(setup, analysis, .4)
    alias = analyze(setup, "session:" + analysis["session_id"])
    assert alias["analysis_id"] == analysis["analysis_id"]
    assert alias["cached_frames"] == 1
    setup.service.stop()
    other = ReplayDetectionService(setup.root, setup.catalog, setup.store, setup.config, setup.engine, setup.gate, setup.replay)
    other._load_model = lambda context: pytest.fail("Cached frame must not load a model")
    try:
        reopened = other.analyze("dataset:1")
        replayed = other.process(reopened["analysis_id"], .4)
        assert reopened["session_id"] == analysis["session_id"]
        assert replayed["cached"] is True
        assert len(setup.store.hazards(analysis["session_id"])) == 2
        assert [h["observations"] for h in replayed["hazards"]] == [1, 1]
        assert first["image_url"] == replayed["image_url"]
    finally:
        other.stop()


def test_config_and_model_fingerprint_create_new_analysis(setup):
    first = analyze(setup)
    setup.client.post("/api/replay/close", json={"analysis_id": first["analysis_id"]})
    setup.config.value["detection"]["confidence"] = .5
    second = analyze(setup)
    assert second["analysis_id"] != first["analysis_id"]
    setup.client.post("/api/replay/close", json={"analysis_id": second["analysis_id"]})
    (setup.root / "best.pt").write_bytes(b"different-model")
    third = analyze(setup)
    assert third["analysis_id"] != second["analysis_id"]


def test_shared_gpu_gate_and_expired_lease_reactivation(setup):
    setup.gate.reserve()
    response = setup.client.post("/api/replay/analyze", json={"source_id": "dataset:1"})
    assert response.status_code == 409
    setup.gate.release()
    analysis = analyze(setup)
    with pytest.raises(RuntimeError): setup.gate.reserve()
    setup.service.last_used = time.monotonic() - 61
    assert not setup.service.active()
    assert not setup.gate.active()
    result = process(setup, analysis, 0)
    assert len(result["hazards"]) == 2
    setup.client.post("/api/replay/close", json={"analysis_id": analysis["analysis_id"]})
    assert not setup.gate.active()


def test_uploaded_gps_is_inherited_as_capture_proxy_without_fake_range(setup):
    setup.store.start_session("uploaded", "upload:drive.avi", setup.config.value)
    setup.store.session_update("uploaded", source_type="upload", source_path=str(setup.path), media_kind="video",
                              original_filename="drive.avi", supplied_latitude=24.3, supplied_longitude=88.6,
                              location_reference="capture", gps_track=[
                                  {"time_s": 0, "latitude": 24.3, "longitude": 88.6, "accuracy_m": 4},
                                  {"time_s": 2, "latitude": 24.32, "longitude": 88.62, "accuracy_m": 6}])
    analysis = analyze(setup, "session:uploaded")
    result = process(setup, analysis, .5)
    for detection in result["detections"]:
        assert detection["latitude"] == pytest.approx(24.305)
        assert detection["distance_m"] is None
        assert detection["location_method"] == "interpolated_capture_location_proxy"
    for hazard in result["hazards"]:
        assert hazard["location_reference"] == "capture"
        assert hazard["distance_m"] is None
    unlocated = process(setup, analysis, 4)
    assert all(detection["latitude"] is None for detection in unlocated["detections"])
    alias = analyze(setup, "session:" + analysis["session_id"])
    assert alias["analysis_id"] == analysis["analysis_id"]


def test_frame_transaction_rolls_back_hazards_and_cache_together(setup, monkeypatch):
    analysis = analyze(setup)
    update = HazardAccumulator.update
    def fail_after_update(*args, **kwargs):
        update(*args, **kwargs)
        raise RuntimeError("Simulated interrupted observation")
    monkeypatch.setattr(HazardAccumulator, "update", fail_after_update)
    response = setup.client.post("/api/replay/process", json={"analysis_id": analysis["analysis_id"], "time_s": 0})
    assert response.status_code == 409
    assert setup.store.hazards(analysis["session_id"]) == []
    assert setup.service.summary(analysis["analysis_id"])["cached_frames"] == 0
    monkeypatch.setattr(HazardAccumulator, "update", update)
    result = process(setup, analysis, 0)
    assert len(result["hazards"]) == 2
    assert all(hazard["observations"] == 1 for hazard in result["hazards"])


def test_empty_predictions_are_cached_without_invented_hazards(setup):
    setup.model.empty = True
    analysis = analyze(setup)
    first = process(setup, analysis, 0)
    second = process(setup, analysis, 0)
    assert first["detections"] == first["hazards"] == []
    assert second["cached"] is True
    assert setup.model.calls == 1


def test_slow_inference_does_not_block_live_engine_snapshots(setup):
    analysis = analyze(setup)
    entered, release, snapped = threading.Event(), threading.Event(), threading.Event()
    predict = setup.model.predict
    def slow_predict(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return predict(*args, **kwargs)
    setup.model.predict = slow_predict
    worker = threading.Thread(target=setup.service.process, args=(analysis["analysis_id"], 0))
    worker.start()
    assert entered.wait(2)
    queued = threading.Thread(target=setup.service.process, args=(analysis["analysis_id"], .2))
    queued.start()
    snapshot = threading.Thread(target=lambda: (setup.engine.snapshot(), snapped.set()))
    snapshot.start()
    try:
        assert snapped.wait(.5), "Replay inference held the engine snapshot lock"
    finally:
        release.set(); worker.join(3); queued.join(3); snapshot.join(3)


@pytest.mark.parametrize("timestamp", [-1, "nan", "inf", 50])
def test_invalid_times_do_not_log_anything(setup, timestamp):
    analysis = analyze(setup)
    response = setup.client.post("/api/replay/process", json={"analysis_id": analysis["analysis_id"], "time_s": timestamp})
    assert response.status_code == 422
    assert setup.store.hazards() == []
