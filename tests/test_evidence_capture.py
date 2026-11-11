from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from roadwatch.engine import HazardAccumulator
from roadwatch.storage import Store


def test_video_evidence_is_spaced_capped_and_retained_after_reopening(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    store = Store(path)
    store.start_session("trip", "file:clip.mp4", {})
    accumulator = HazardAccumulator("trip", store)
    detection = {"track_id": 7, "class_name": "Pothole", "confidence": .8, "bbox": [5, 5, 40, 40]}
    _, jpeg = cv2.imencode(".jpg", np.zeros((60, 80, 3), np.uint8))
    settings = {"match_radius_m": 12, "time_tolerance_s": 5}
    for index in range(200):
        timestamp = index / 20
        accumulator.update(detection, timestamp, None, [], settings)
        accumulator.save_frame([detection, detection], jpeg.tobytes(), index, timestamp, 80, 60)
    reopened = Store(path)
    evidence = reopened.evidence("trip:7")
    assert len(evidence) == 10
    assert len({frame["frame_index"] for frame in evidence}) == 10
    assert all(b["time_s"] - a["time_s"] >= .5 for a, b in zip(evidence, evidence[1:]))
    hazard = reopened.hazard("trip:7")
    assert hazard["observations"] == 200
    assert hazard["evidence_count"] == 10
    assert hazard["thumbnail_url"] == evidence[0]["url"]
    image, metadata = reopened.evidence_image(evidence[0]["id"])
    assert cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR).shape == (60, 80, 3)
    assert metadata["bbox"] == [5, 5, 40, 40]


def test_unconfirmed_detections_never_create_evidence(tmp_path):
    store = Store(tmp_path / "evidence.sqlite3")
    accumulator = HazardAccumulator("trip", store)
    accumulator.save_frame([{"track_id": None}], b"jpeg", 0, 0, 80, 60)
    assert store.hazards() == []


def test_hazard_preserves_box_and_native_video_clock_when_observed_out_of_order(tmp_path):
    store = Store(tmp_path / "evidence.sqlite3")
    store.start_session("trip", "dataset:33", {})
    accumulator = HazardAccumulator("trip", store)
    settings = {"match_radius_m": 12, "time_tolerance_s": 5}
    detection = {"track_id": 7, "class_name": "Pothole", "confidence": .8,
                 "bbox": [5, 5, 40, 40], "frame_width": 80, "frame_height": 60,
                 "source_type": "dataset", "frame_index": 100, "video_time_s": 5}
    accumulator.update(detection, 42, "33", [], settings)
    detection.update(frame_index=60, video_time_s=3, bbox=[4, 4, 30, 30])
    accumulator.update(detection, 40, "33", [], settings)
    saved = store.hazard("trip:7")
    assert saved["first_seen_s"] == 40
    assert saved["last_seen_s"] == 42
    assert saved["first_video_time_s"] == 3
    assert saved["last_video_time_s"] == 5
    assert saved["bbox"] == [4, 4, 30, 30]
    assert saved["frame_width"] == 80
    assert saved["source_type"] == "dataset"
