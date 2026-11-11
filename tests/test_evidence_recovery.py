"""Recover real local media without manufacturing original tracker evidence."""
import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import cv2
from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np
import pytest

from roadwatch.config import ConfigStore
from roadwatch.evidence_recovery import EvidenceRecoveryService, make_evidence_recovery_router, recovery_times
from roadwatch.library_api import make_library_router
from roadwatch.replay import ReplayService
from roadwatch.storage import Store


class Lease:
    def __init__(self):
        self.busy = False
        self.reservations = 0
        self.releases = 0

    def reserve(self):
        if self.busy:
            raise RuntimeError("Another inference job is using the model")
        self.busy = True
        self.reservations += 1

    def release(self):
        self.busy = False
        self.releases += 1


class Array:
    def __init__(self, values):
        self.values = values

    def cpu(self):
        return self

    def tolist(self):
        return self.values


class FakeModel:
    names = {0: "Pothole", 1: "Transverse"}

    def __init__(self):
        self.calls = 0

    def predict(self, frame, **kwargs):
        self.calls += 1
        return [SimpleNamespace(boxes=SimpleNamespace(xyxy=Array([[8, 8, 38, 40], [40, 5, 65, 40]]),
                                                     cls=Array([0, 1]), conf=Array([.8, .7])))]


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    video = tmp_path / "source.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (80, 60))
    assert writer.isOpened()
    for index in range(10):
        writer.write(np.full((60, 80, 3), 60 + 10 * index, dtype=np.uint8))
    writer.release()
    storage = Store(tmp_path / "evidence.sqlite3")
    storage.start_session("old", "dataset:33", {"sync_offset_s": 37})
    hazard = {"id": "old:1", "session_id": "old", "recording_id": "33", "dataset_id": "roadsens",
              "class_name": "Pothole", "confidence": .9, "first_seen_s": 37.2, "last_seen_s": 37.6,
              "latitude": 1., "longitude": 2., "track_id": 1, "observations": 5}
    storage.save_hazard("old", hazard)
    catalog = SimpleNamespace(recording=lambda identity: {"id": "33", "video_path": str(video)} if identity == "33" else None)
    replay = ReplayService(tmp_path, catalog, storage)
    config = ConfigStore(tmp_path / "config.json")
    lease = Lease()
    engine = SimpleNamespace(active=lambda: False, lock=threading.RLock())
    service = EvidenceRecoveryService(tmp_path, storage, catalog, replay, config, engine, lease)
    model = FakeModel()
    model_loads = []

    def make_model(configuration):
        model_loads.append(configuration)
        return model

    monkeypatch.setattr(service, "_make_model", make_model)
    app = FastAPI()
    # Literal evidence-status must precede the generic hazard detail route.
    app.include_router(make_evidence_recovery_router(service))
    app.include_router(make_library_router(storage, tmp_path))
    with TestClient(app) as client:
        yield SimpleNamespace(root=tmp_path, storage=storage, hazard=hazard, replay=replay,
                              service=service, client=client, config=config, lease=lease,
                              model=model, loads=model_loads, video=video, catalog=catalog)
    service.stop()
    replay.close()


def finished(recovery, response):
    assert response.status_code == 202, response.text
    identity = response.json()["id"]
    if recovery.service.worker:
        recovery.service.worker.join(5)
        assert not recovery.service.worker.is_alive()
    result = recovery.client.get("/api/library/evidence-jobs/" + identity)
    assert result.status_code == 200
    return result.json()


def test_sensor_clock_is_undone_only_for_legacy_roadsens():
    hazard = {"first_seen_s": 37.2, "last_seen_s": 37.6, "recording_id": "33"}
    session = {"source": "dataset:33", "config": {"sync_offset_s": 37}}
    assert recovery_times(hazard, session, 1) == [.2, .4, .6]
    hazard.update(first_video_time_s=.1, last_video_time_s=.5)
    assert recovery_times(hazard, session, 1) == [.1, .3, .5]
    comma = {"first_seen_s": .2, "last_seen_s": .6, "recording_id": "comma2k19-test"}
    assert recovery_times(comma, {"source": "dataset:comma2k19-test", "config": {"sync_offset_s": 37}}, 1) == [.2, .4, .6]
    uploaded = {"first_seen_s": .2, "last_seen_s": .6, "source_type": "upload_video"}
    assert recovery_times(uploaded, {"source": "upload:video", "config": {"sync_offset_s": 37}}, 1) == [.2, .4, .6]
    assert recovery_times({"first_seen_s": -10, "last_seen_s": 100}, {"source": "file:clip"}, 1) == [0, .999999]
    with pytest.raises(ValueError, match="timestamp"):
        recovery_times({}, {}, 1)


def test_actual_legacy_video_frames_are_saved_without_changing_hazard_history(recovery):
    original = copy.deepcopy(recovery.hazard)
    status = recovery.client.get("/api/library/evidence-status").json()
    assert status["missing"] == status["recoverable"] == 1
    job = finished(recovery, recovery.client.post("/api/library/old:1/recover-evidence"))
    assert job["status"] == "completed" and job["recovered"] == 1 and job["failed"] == 0
    assert len(recovery.loads) == 1 and recovery.model.calls == 3
    images = recovery.storage.evidence("old:1")
    assert [image["time_s"] for image in images] == [.2, .4, .6]
    assert [image["frame_index"] for image in images] == [2, 4, 6]
    for image in images:
        jpeg, metadata = recovery.storage.evidence_image(image["id"])
        assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape == (60, 80, 3)
        assert metadata["source"] == "recovered_source_frame" and metadata["reconstructed"]
        assert metadata["overlay_origin"] == "current_model_prediction"
        assert not metadata["original_track_association_verified"]
        assert len(metadata["overlays"]) == 1
        assert metadata["overlays"][0]["class_name"] == "Pothole"
    saved = recovery.storage.hazard("old:1")
    assert all(saved[key] == value for key, value in original.items())
    assert saved["evidence_count"] == 3 and saved["thumbnail_url"]
    assert recovery.client.get(saved["thumbnail_url"]).status_code == 200
    assert not recovery.lease.busy and recovery.lease.reservations == recovery.lease.releases == 1
    assert recovery.service.status()["missing"] == 0


def test_stored_box_only_applies_to_saved_last_observation_and_scales(recovery):
    hazard = {**recovery.hazard, "bbox": [20, 20, 80, 80], "frame_width": 160, "frame_height": 120}
    recovery.storage.save_hazard("old", hazard)
    finished(recovery, recovery.client.post("/api/library/old:1/recover-evidence"))
    images = recovery.storage.evidence("old:1")
    assert images[0]["overlay_origin"] == images[1]["overlay_origin"] == "current_model_prediction"
    assert images[2]["overlay_origin"] == "recovered_original_bbox"
    assert images[2]["bbox"] == [10, 10, 40, 40]


def test_photos_are_recovered_from_actual_upload_and_remain_idempotent(recovery):
    path = recovery.root / "photo.jpg"
    assert cv2.imwrite(str(path), np.full((100, 120, 3), 150, dtype=np.uint8))
    recovery.storage.start_session("photo", "upload:photo", {})
    recovery.storage.session_update("photo", media_kind="photo", source_type="upload", source_path=str(path))
    recovery.storage.save_hazard("photo", {"id": "photo:1", "session_id": "photo", "class_name": "Transverse", "source_type": "upload_photo"})
    job = finished(recovery, recovery.client.post("/api/library/photo:1/recover-evidence"))
    assert job["recovered"] == 1
    images = recovery.storage.evidence("photo:1")
    assert len(images) == 1 and images[0]["time_s"] == 0
    assert images[0]["overlays"][0]["class_name"] == "Transverse"
    again = finished(recovery, recovery.client.post("/api/library/photo:1/recover-evidence"))
    assert again["skipped"] == 1
    assert len(recovery.storage.evidence("photo:1")) == 1


def test_original_missing_and_external_paths_are_explicitly_unavailable(recovery, tmp_path):
    recovery.storage.start_session("missing", "file:" + str(recovery.root / "absent.avi"), {})
    recovery.storage.save_hazard("missing", {"id": "missing:1", "session_id": "missing", "class_name": "Pothole", "first_seen_s": 0})
    outside = recovery.root.parent / (recovery.root.name + "-external.jpg")
    try:
        cv2.imwrite(str(outside), np.zeros((40, 40, 3), np.uint8))
        recovery.storage.start_session("external", "upload:photo", {})
        recovery.storage.session_update("external", media_kind="photo", source_path=str(outside))
        recovery.storage.save_hazard("external", {"id": "external:1", "session_id": "external", "class_name": "Pothole"})
        status = recovery.service.status()
        assert status["unavailable"] == 2
        assert all(item["reason"] for item in status["items"] if not item["recoverable"])
        job = finished(recovery, recovery.client.post("/api/library/recover-evidence", json={"hazard_ids": ["missing:1", "external:1"]}))
        assert job["failed"] == 2 and job["recovered"] == 0
        assert not recovery.storage.evidence("missing:1") and not recovery.storage.evidence("external:1")
        assert recovery.service.status()["unavailable"] == 2
    finally:
        outside.unlink(missing_ok=True)


def test_failed_model_does_not_invent_boxes_or_prevent_real_source_images(recovery, monkeypatch):
    def unavailable(config):
        raise ValueError("Model file unavailable")

    monkeypatch.setattr(recovery.service, "_make_model", unavailable)
    job = finished(recovery, recovery.client.post("/api/library/old:1/recover-evidence"))
    assert job["recovered"] == 1
    for image in recovery.storage.evidence("old:1"):
        assert image["overlay_origin"] == "no_original_bbox"
        assert image["bbox"] is None and image["overlays"] == []
        assert "unavailable" in image["overlay_error"]


def test_recovery_keeps_existing_evidence_within_sqlite_cap(recovery):
    for index in range(10):
        recovery.storage.save_evidence("old:1", b"existing JPEG", {"frame_index": 100 + index})
    job = finished(recovery, recovery.client.post("/api/library/old:1/recover-evidence"))
    assert job["skipped"] == 1 and len(recovery.storage.evidence("old:1")) == 10
    assert not recovery.loads


def test_busy_inference_guard_and_input_limits(recovery):
    recovery.lease.busy = True
    assert recovery.client.post("/api/library/old:1/recover-evidence").status_code == 409
    recovery.lease.busy = False
    assert recovery.client.post("/api/library/missing/recover-evidence").status_code == 404
    assert recovery.client.post("/api/library/recover-evidence", json={"limit": 101}).status_code == 422
    assert recovery.client.post("/api/library/recover-evidence", json={"limit": True}).status_code == 422
    assert recovery.client.post("/api/library/recover-evidence", json={"hazard_ids": "old:1"}).status_code == 422
    assert recovery.client.get("/api/library/evidence-jobs/missing").status_code == 404


def test_job_and_failure_reasons_survive_service_restart(recovery):
    recovery.storage.start_session("missing", "camera:0", {})
    recovery.storage.save_hazard("missing", {"id": "missing:1", "session_id": "missing", "class_name": "Pothole", "first_seen_s": 0})
    job = finished(recovery, recovery.client.post("/api/library/missing:1/recover-evidence"))
    assert job["failed"] == 1
    restarted = EvidenceRecoveryService(recovery.root, recovery.storage, recovery.catalog, recovery.replay,
                                        recovery.config, SimpleNamespace(), recovery.lease)
    assert restarted.job(job["id"])["results"][0]["reason"]
    assert restarted.status()["items"][0]["status"] == "unavailable"
    with recovery.storage.connect() as db:
        interrupted = {"id": "interrupted", "status": "processing", "processed": 1, "recovered": 1}
        db.execute("INSERT INTO evidence_recovery_jobs(id,payload) VALUES(?,?)", ("interrupted", json.dumps(interrupted)))
    restarted_again = EvidenceRecoveryService(recovery.root, recovery.storage, recovery.catalog, recovery.replay,
                                              recovery.config, SimpleNamespace(), recovery.lease)
    assert restarted_again.job("interrupted")["status"] == "error"
    assert "restart" in restarted_again.job("interrupted")["message"]


def test_library_defaults_to_real_photographs_and_filters_missing_evidence(recovery):
    recovery.storage.save_evidence("old:1", b"JPEG", {"frame_index": 0})
    recovery.storage.save_hazard("old", {"id": "old:2", "class_name": "Pothole", "latitude": 1.00001, "longitude": 2.})
    default = recovery.client.get("/api/library").json()
    assert [item["id"] for item in default["hazards"]] == ["old:1", "old:2"]
    recent = recovery.client.get("/api/library", params={"evidence_first": False}).json()
    assert [item["id"] for item in recent["hazards"]] == ["old:2", "old:1"]
    assert recovery.client.get("/api/library", params={"has_evidence": True}).json()["total"] == 1
    assert recovery.client.get("/api/library", params={"has_evidence": False}).json()["hazards"][0]["id"] == "old:2"
    nearest = recovery.client.get("/api/library", params={"latitude": 1.00001, "longitude": 2}).json()
    assert [item["id"] for item in nearest["hazards"]] == ["old:2", "old:1"]
