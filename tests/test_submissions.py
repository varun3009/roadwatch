import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from roadwatch.config import defaults
from roadwatch.storage import Store
from roadwatch.submissions import SubmissionService, make_submission_router, position_at, validate_submission


class Tensor:
    def __init__(self, values):
        self.values = values

    def cpu(self):
        return self

    def tolist(self):
        return self.values


class FakeModel:
    names = {0: "Pothole", 1: "Longitudinal"}

    def __init__(self, empty=False):
        self.empty = empty
        self.predict_calls = 0
        self.track_calls = 0

    def result(self, tracked=False):
        class Boxes(SimpleNamespace):
            def __len__(self):
                return len(self.xyxy.values)
        boxes = Boxes(xyxy=Tensor([[10., 10., 50., 45.], [65., 15., 100., 50.]]),
                                cls=Tensor([0., 1.]), conf=Tensor([.9, .8]), id=Tensor([1., 2.]) if tracked else None)
        return [SimpleNamespace(boxes=None if self.empty else boxes)]

    def predict(self, frame, **kwargs):
        self.predict_calls += 1
        return self.result()

    def track(self, frame, **kwargs):
        self.track_calls += 1
        return self.result(tracked=True)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "reports.sqlite3")
    config = defaults()
    config["detection"]["frame_stride"] = 1
    engine = SimpleNamespace(lock=threading.RLock(), active=lambda: False)
    service = SubmissionService(tmp_path, store, SimpleNamespace(value=config), engine)
    model = FakeModel()
    service._model = lambda config: model
    app = FastAPI()
    app.include_router(make_submission_router(service))
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, store=store, service=service, model=model, root=tmp_path)
    service.stop()


def fields(**extra):
    return {"mode": "detect", "latitude": "24.3", "longitude": "88.6", **extra}


def image_bytes():
    success, encoded = cv2.imencode(".jpg", np.full((80, 120, 3), 140, dtype=np.uint8))
    assert success
    return encoded.tobytes()


def completed(setup, response):
    assert response.status_code == 202, response.text
    identity = response.json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = setup.client.get("/api/submissions/" + identity).json()
        if job["status"] in {"completed", "error"}:
            return job
        time.sleep(.01)
    raise AssertionError("Job did not finish")


def test_photo_predictions_save_distinct_hazards_with_evidence_and_provenance(setup):
    response = setup.client.post("/api/submissions", data=fields(location_reference="capture", notes="Please inspect", address="Road 1"),
                                 files={"file": ("../../outside.jpg", image_bytes(), "image/jpeg")})
    job = completed(setup, response)
    assert job["status"] == "completed"
    assert setup.model.predict_calls == 1
    assert setup.model.track_calls == 0
    assert len(job["hazards"]) == 2
    for hazard in job["hazards"]:
        assert hazard["evidence_count"] == 1
        assert hazard["distance_m"] is None
        assert hazard["latitude"] == 24.3
        assert hazard["location_method"] == "fixed_capture_location_proxy"
        assert "whole upload" in hazard["location_note"]
        assert hazard["notes"] == "Please inspect"
        evidence = setup.store.evidence(hazard["id"])[0]
        encoded, _ = setup.store.evidence_image(evidence["id"])
        assert cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR) is not None
    session = setup.store.session(job["session_id"])
    assert session["metadata"]["original_filename"] == "outside.jpg"
    assert Path(session["metadata"]["source_path"]).parent == setup.service.upload_root


def test_no_predictions_never_invents_hazard(setup):
    setup.model.empty = True
    job = completed(setup, setup.client.post("/api/submissions", data=fields(), files={"file": ("road.jpg", image_bytes(), "image/jpeg")}))
    assert job["status"] == "completed"
    assert job["hazards"] == []
    assert "No supported hazards" in job["message"]


def test_manual_report_without_media_survives_new_service(setup):
    response = setup.client.post("/api/submissions", data=fields(mode="manual", hazard_type="Open manhole", location_reference="hazard", severity="high"))
    job = completed(setup, response)
    assert len(job["hazards"]) == 1
    hazard = job["hazards"][0]
    assert hazard["class_name"] == "Open manhole"
    assert hazard["confidence"] is None
    assert hazard["evidence_count"] == 0
    assert hazard["location_method"] == "user_supplied_hazard_location"
    restored = SubmissionService(setup.root, setup.store, setup.service.config_store, setup.service.engine)
    recovered = restored.job(job["id"])
    assert recovered["status"] == "completed"
    assert recovered["hazards"][0]["id"] == hazard["id"]


def test_manual_photo_does_not_run_model(setup):
    response = setup.client.post("/api/submissions", data=fields(mode="manual", hazard_type="Drain damage"),
                                 files={"file": ("evidence.jpg", image_bytes(), "image/jpeg")})
    job = completed(setup, response)
    assert job["status"] == "completed"
    assert len(job["hazards"]) == 1
    assert job["hazards"][0]["evidence_count"] == 1
    assert setup.model.predict_calls == 0


@pytest.mark.parametrize("values", [fields(latitude="nan"), fields(longitude="181"), fields(latitude=""),
                                    fields(mode="manual", hazard_type=""), fields(severity="urgent"),
                                    fields(location_reference="automatic"), fields(gps_track="not json")])
def test_invalid_submission_is_rejected_before_upload(setup, values):
    response = setup.client.post("/api/submissions", data=values)
    assert response.status_code == 422
    assert setup.store.hazards() == []
    assert list(setup.service.upload_root.iterdir()) == []
    assert not setup.service.active()


def test_upload_limits_delete_partial_file_and_release_reservation(setup, monkeypatch):
    monkeypatch.setattr("roadwatch.submissions.MAX_PHOTO_BYTES", 10)
    response = setup.client.post("/api/submissions", data=fields(), files={"file": ("huge.jpg", b"x" * 11, "image/jpeg")})
    assert response.status_code == 413
    assert list(setup.service.upload_root.iterdir()) == []
    assert not setup.service.active()


def test_invalid_media_becomes_explicit_failed_job_without_reports(setup):
    job = completed(setup, setup.client.post("/api/submissions", data=fields(), files={"file": ("bad.jpg", b"bad JPEG", "image/jpeg")}))
    assert job["status"] == "error"
    assert job["error"]
    assert job["hazards"] == []
    assert not setup.service.active()


def test_existing_engine_or_upload_job_rejects_new_submission(setup):
    setup.service.engine.active = lambda: True
    response = setup.client.post("/api/submissions", data=fields(mode="manual", hazard_type="Pothole"))
    assert response.status_code == 409
    setup.service.engine.active = lambda: False
    setup.service.reserve()
    response = setup.client.post("/api/submissions", data=fields(mode="manual", hazard_type="Pothole"))
    assert response.status_code == 409
    setup.service.release()


def test_gps_track_interpolation_wrap_gap_and_no_extrapolation():
    values = validate_submission(fields(gps_track=json.dumps([
        {"time_s": 0, "latitude": 0, "longitude": 179, "accuracy_m": 2},
        {"time_s": 2, "latitude": 2, "longitude": -179, "accuracy_m": 3},
        {"time_s": 9, "latitude": 3, "longitude": -178},
    ])))
    assert position_at(values, 1) == {"latitude": 1, "longitude": -180, "gps_accuracy_m": 3}
    assert position_at(values, 5) is None
    assert position_at(values, 10) is None
    assert position_at(values, -1) is None
    assert position_at(values, 9)["gps_accuracy_m"] is None
    assert values["location_reference"] == "capture"
    with pytest.raises(ValueError, match="strictly increasing"):
        validate_submission(fields(gps_track=[{"time_s": 0, "latitude": 1, "longitude": 1}] * 2))


def test_video_tracks_each_hazard_with_hard_ten_frame_limit(setup, tmp_path):
    path = tmp_path / "fixture.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 20, (120, 80))
    if not writer.isOpened():
        pytest.skip("MJPG encoder unavailable")
    for index in range(40):
        writer.write(np.full((80, 120, 3), 60 + index, np.uint8))
    writer.release()
    response = setup.client.post("/api/submissions", data=fields(), files={"file": ("drive.avi", path.read_bytes(), "video/x-msvideo")})
    job = completed(setup, response)
    assert job["status"] == "completed", job.get("error")
    assert setup.model.track_calls == 40
    assert len(job["hazards"]) == 2
    assert all(hazard["observations"] == 40 and hazard["evidence_count"] == 10 for hazard in job["hazards"])
    assert job["progress"] == 100
    assert setup.store.session(job["session_id"])["metadata"]["sampled_frames"] == 40


@pytest.mark.parametrize("use_calibration", [False, True])
def test_video_detection_without_gps_uses_calibration_only_when_requested(setup, use_calibration):
    profile = setup.service.config_store.value["calibration"]
    profile.update(width=120, height=80, fx=100, fy=100, cx=60, cy=40, height_m=1, pitch_deg=0,
                   verified=True)
    path = setup.root / "fixture.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (120, 80))
    assert writer.isOpened()
    for _ in range(3):
        writer.write(np.zeros((80, 120, 3), np.uint8))
    writer.release()
    response = setup.client.post("/api/submissions", data={"use_calibration": str(use_calibration).lower()},
                                 files={"file": ("drive.avi", path.read_bytes(), "video/x-msvideo")})
    job = completed(setup, response)
    assert job["status"] == "completed", job.get("error")
    pothole = next(item for item in job["hazards"] if item["class_name"] == "Pothole")
    assert pothole["latitude"] is None and pothole["longitude"] is None
    assert pothole["location_method"] is None
    assert pothole["distance_m"] == (pytest.approx((20**2 + 6**2)**.5) if use_calibration else None)
    session = setup.store.session(job["session_id"])
    assert session["metadata"]["use_calibration"] is use_calibration
    if use_calibration:
        assert session["config"]["calibration"]["height_m"] == 1
        assert pothole["provisional"] is False


def test_photo_rejects_video_calibration_option(setup):
    response = setup.client.post("/api/submissions", data=fields(use_calibration="true"),
                                 files={"file": ("road.jpg", image_bytes(), "image/jpeg")})
    assert response.status_code == 422
    assert not setup.service.active()
    assert not list(setup.service.upload_root.iterdir())


def test_unknown_or_interrupted_job_has_honest_status(setup):
    assert setup.client.get("/api/submissions/no-such-job").status_code == 404
    setup.store.start_session("interrupted", "upload:test.mp4", defaults())
    setup.store.session_update("interrupted", submission={"id": "interrupted", "status": "processing", "progress": 25})
    recovered = setup.service.job("interrupted")
    assert recovered["status"] == "error"
    assert "restart" in recovered["error"]
