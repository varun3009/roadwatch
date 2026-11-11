"""Engine timing, evidence retention and pose tests without model inference."""

import copy
import math
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from roadwatch.config import defaults
import numpy as np

from roadwatch.engine import Engine, HazardAccumulator, _frame_time_s, _resize_for_processing
from roadwatch.geometry import calibration_from_dict, locate_hazard, project_pixel
from roadwatch.storage import Store


SETTINGS = {"match_radius_m": 12, "time_tolerance_s": 5}


def detection(track=1, **changes):
    return {"class_name": "pothole", "track_id": track, "confidence": 0.9,
            "distance_m": 10.0, "latitude": None, "longitude": None,
            "gps_accuracy_m": None, "provisional": True, **changes}


class AccumulatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "evidence.sqlite3")
        self.store.start_session("session", "test", {})
        self.accumulator = HazardAccumulator("session", self.store, max_items=2)

    def tearDown(self):
        self.directory.cleanup()

    def test_bounded_cache_reloads_evicted_track_without_duplicate_or_count_loss(self):
        for track in (1, 2, 3):
            self.accumulator.update(detection(track), track, "34", [], SETTINGS)
        self.assertEqual(len(self.accumulator.items), 2)
        self.assertNotIn(1, self.accumulator.items)
        returned = self.accumulator.update(detection(1), 8, "34", [], SETTINGS)
        self.assertEqual(returned["observations"], 2)
        self.assertEqual(returned["first_seen_s"], 1)
        self.assertEqual(returned["last_seen_s"], 8)
        self.assertEqual(len(self.accumulator.items), 2)
        self.assertEqual(len(self.store.hazards("session")), 3)

    def test_unlocated_hazard_remains_evidence_without_invented_gps(self):
        event = self.accumulator.update(detection(), 1, "34", [], SETTINGS)
        self.assertEqual(event["observations"], 1)
        self.assertEqual(event["located_observations"], 0)
        self.assertIsNone(event["latitude"])
        self.assertIsNone(event["longitude"])
        self.assertEqual(event["distance_m"], 10)
        self.assertEqual(event["distance_kind"], "horizontal_from_gps_antenna")
        self.assertEqual(self.store.hazards("session")[0]["id"], event["id"])

    def test_same_track_with_noisy_classes_remains_one_hazard_with_strongest_label(self):
        observations = [
            ("Pothole", 0.45), ("Pothole", 0.537),
            ("Alligator", 0.486), ("Alligator", 0.40),
            ("Longitudinal", 0.388), ("Longitudinal", 0.35), ("Longitudinal", 0.30),
        ]
        for second, (label, confidence) in enumerate(observations):
            event = self.accumulator.update(detection(6, class_name=label, confidence=confidence), second, "33", [], SETTINGS)
        self.assertEqual(len(self.store.hazards("session")), 1)
        self.assertEqual(len(self.accumulator.items), 1)
        self.assertEqual(event["id"], "session:6")
        self.assertEqual(event["class_name"], "Pothole")
        self.assertEqual(event["hazard_type"], "Pothole")
        self.assertEqual(event["confidence"], 0.537)
        self.assertEqual(event["observations"], 7)
        self.assertTrue(event["class_ambiguous"])
        self.assertEqual({name: values["observations"] for name, values in event["class_evidence"].items()}, {"Pothole": 2, "Alligator": 2, "Longitudinal": 3})
        self.assertEqual(event["class_evidence"]["Alligator"]["max_confidence"], 0.486)

    def test_stable_class_family_change_discards_incompatible_reference(self):
        reference = {"id": "pothole-ref", "recording_id": "33", "time_s": 1, "hazard_type": "pothole"}
        first = detection(6, class_name="Pothole", confidence=0.5)
        event = self.accumulator.update(first, 1, "33", [reference], SETTINGS)
        self.assertIsNotNone(event["match"])
        noisy = detection(6, class_name="Alligator", confidence=0.4)
        event = self.accumulator.update(noisy, 2, "33", [reference], SETTINGS)
        self.assertEqual(event["class_name"], "Pothole")
        self.assertIsNotNone(event["match"])
        self.assertIsNone(noisy["match"])
        stronger = detection(6, class_name="Alligator", confidence=0.7)
        event = self.accumulator.update(stronger, 3, "33", [reference], SETTINGS)
        self.assertEqual(event["class_name"], "Alligator")
        self.assertIsNone(event["match"])
        self.assertNotIn("matched_at_s", event)

    def test_compatible_fusion_retains_gps_bias_floor_and_rejects_distant_switch(self):
        first = detection(latitude=43.0, longitude=-79.0, gps_accuracy_m=8)
        self.accumulator.update(first, 1, "34", [], SETTINGS)
        near = detection(latitude=43.00001, longitude=-79.0, gps_accuracy_m=3)
        event = self.accumulator.update(near, 2, "34", [], SETTINGS)
        self.assertAlmostEqual(event["latitude"], 43.000005)
        self.assertEqual(event["gps_accuracy_m"], 8)
        far = detection(latitude=44.0, longitude=-79.0, gps_accuracy_m=2)
        event = self.accumulator.update(far, 3, "34", [], SETTINGS)
        self.assertEqual(event["observations"], 3)
        self.assertEqual(event["located_observations"], 2)
        self.assertEqual(event["rejected_location_observations"], 1)
        self.assertAlmostEqual(event["latitude"], 43.000005)

    def test_dateline_fusion_and_unknown_accuracy(self):
        self.accumulator.update(detection(latitude=0, longitude=179.99999), 1, "34", [], SETTINGS)
        event = self.accumulator.update(detection(latitude=0, longitude=-179.99999, gps_accuracy_m=4), 2, "34", [], SETTINGS)
        self.assertGreater(abs(event["longitude"]), 179.9999)
        self.assertIsNone(event["gps_accuracy_m"])
        self.assertEqual(event["max_reported_gps_accuracy_m"], 4)

    def test_invalid_coordinate_does_not_crash_or_persist_nonfinite_number(self):
        event = self.accumulator.update(detection(latitude=math.nan, longitude=0), 1, "34", [], SETTINGS)
        self.assertEqual(event["located_observations"], 0)
        self.assertIsNone(self.store.hazards()[0]["latitude"])

    def test_spatial_workbook_match_without_recording_and_incompatible_recording_rejected(self):
        reference = {"id": "excel-event", "recording_id": None, "hazard_type": "pothole", "latitude": 43, "longitude": -79}
        event = self.accumulator.update(detection(latitude=43, longitude=-79), 1, "34", [reference], SETTINGS)
        self.assertEqual(event["match"]["reference_id"], "excel-event")
        incompatible = {**reference, "id": "other-trip", "recording_id": "99"}
        event = self.accumulator.update(detection(2, latitude=43, longitude=-79), 1, "34", [incompatible], SETTINGS)
        self.assertIsNone(event["match"])

    def test_temporal_match_is_retained_after_later_views_leave_annotation_window(self):
        reference = {"id": "annotation", "recording_id": "34", "time_s": 2, "hazard_type": "pothole", "latitude": None, "longitude": None}
        event = self.accumulator.update(detection(), 2, "34", [reference], SETTINGS)
        self.assertEqual(event["match"]["match_kind"], "temporal")
        event = self.accumulator.update(detection(), 20, "34", [reference], SETTINGS)
        self.assertEqual(event["match"]["reference_id"], "annotation")
        self.assertEqual(event["matched_at_s"], 2)


class FakeCatalog:
    def __init__(self):
        self.pose = {"latitude": 43, "longitude": -79, "heading_deg": 0, "accuracy_m": 5}
        self.last_query = None

    def pose_at(self, recording_id, sensor_time, max_gap_s):
        self.last_query = (recording_id, sensor_time, max_gap_s)
        return copy.deepcopy(self.pose)


class PoseAndTimelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "evidence.sqlite3")
        self.config = defaults()
        self.catalog = FakeCatalog()
        self.engine = Engine(Path(self.directory.name), None, self.catalog, self.store)

    def tearDown(self):
        self.directory.cleanup()

    def test_dataset_uses_requested_sensor_time_and_null_heading_cannot_locate(self):
        pose = self.engine._pose("dataset", "34", 12.5, self.config)
        self.assertEqual(self.catalog.last_query, ("34", 12.5, 3))
        self.assertEqual(pose["heading_deg"], 0)
        self.catalog.pose["heading_deg"] = None
        pose = self.engine._pose("dataset", "34", 12.5, self.config)
        self.assertIsNone(pose)
        calibration = calibration_from_dict(self.config["calibration"])
        projected = project_pixel(360, 800, 720, 1280, calibration)
        location = locate_hazard(projected, pose, calibration)
        self.assertFalse(location["valid"])
        self.assertIsNone(location["latitude"])

    def test_manual_pose_is_explicit_fixed_position_and_requires_all_coordinates(self):
        self.config["pose"].update(latitude=43, longitude=-79, heading_deg=90)
        pose = self.engine._pose("manual", None, 9, self.config)
        self.assertEqual(pose["source"], "manual fixed vehicle position")
        self.assertEqual(pose["time_s"], 9)
        self.config["pose"]["longitude"] = None
        self.assertIsNone(self.engine._pose("manual", None, 9, self.config))

    def test_live_fix_freshness_and_capture_time_are_both_required(self):
        with patch("roadwatch.engine.time.monotonic", return_value=100):
            self.engine.set_pose(self.catalog.pose)
        with patch("roadwatch.engine.time.monotonic", return_value=102):
            pose = self.engine._pose("live", None, 2, self.config, frame_monotonic=101)
            self.assertEqual(pose["fix_time_basis"], "server receipt")
            self.assertNotIn("received_monotonic", pose)
            self.assertIsNone(self.engine._pose("live", None, 2, self.config, frame_monotonic=90))
        with patch("roadwatch.engine.time.monotonic", return_value=104):
            self.assertIsNone(self.engine._pose("live", None, 4, self.config))

    def test_old_producer_timestamp_is_rejected_even_when_packet_just_arrived(self):
        with patch("roadwatch.engine.time.monotonic", return_value=100), patch("roadwatch.engine.time.time", return_value=1000):
            self.engine.set_pose({**self.catalog.pose, "timestamp_ms": 990_000})
            self.assertIsNone(self.engine._pose("live", None, 1, self.config, frame_monotonic=100))
            self.engine.set_pose({**self.catalog.pose, "timestamp_ms": 999_000})
            pose = self.engine._pose("live", None, 1, self.config, frame_monotonic=100)
            self.assertEqual(pose["fix_time_basis"], "producer timestamp")
            self.assertEqual(pose["fix_age_s"], 1)
            with self.assertRaises(ValueError):
                self.engine.set_pose({**self.catalog.pose, "timestamp_ms": math.nan})
            with self.assertRaises(ValueError):
                self.engine.set_pose({**self.catalog.pose, "timestamp_ms": 1002_000})

    def test_repeated_missing_or_regressing_decoder_timestamps_advance(self):
        self.assertEqual(_frame_time_s(0, 30, 0), 0)
        self.assertAlmostEqual(_frame_time_s(3, 30, 0, 0), 0.1)
        self.assertAlmostEqual(_frame_time_s(6, 30, math.nan, 0.1), 0.2)
        self.assertAlmostEqual(_frame_time_s(9, 30, 50, 0.2), 0.3)
        self.assertAlmostEqual(_frame_time_s(12, 30, 450, 0.3), 0.45)

    def test_large_portrait_frame_is_uniformly_resized_without_orientation_change(self):
        frame = np.zeros((3840, 2160, 3), np.uint8)
        resized = _resize_for_processing(frame)
        self.assertEqual(resized.shape, (1280, 720, 3))
        small = np.zeros((480, 640, 3), np.uint8)
        self.assertIs(_resize_for_processing(small), small)

    def test_recording_grabs_all_frames_retrieves_only_stride_and_keeps_source_timeline(self):
        class FakeCapture:
            def __init__(self):
                self.index = -1
                self.retrieved = []
                self.released = False

            def open(self, source):
                return True

            def isOpened(self):
                return True

            def set(self, key, value):
                return True

            def get(self, key):
                import cv2
                return 30.0 if key == cv2.CAP_PROP_FPS else self.index * 1000 / 30

            def read(self):
                raise AssertionError("Skipped recording frames must not use read()")

            def grab(self):
                self.index += 1
                return self.index < 8

            def retrieve(self):
                self.retrieved.append(self.index)
                return True, np.full((1280, 720, 3), self.index, np.uint8)

            def release(self):
                self.released = True

        frames = []

        class FakeModel:
            names = {0: "Pothole"}

            def track(self, frame, **kwargs):
                frames.append(int(frame[0, 0, 0]))
                return [SimpleNamespace(boxes=None)]

        capture = FakeCapture()
        self.store.start_session("pipeline", "fake file", self.config)
        with patch.dict("sys.modules", {"ultralytics": SimpleNamespace(YOLO=lambda path: FakeModel())}), \
                patch("roadwatch.engine.cv2.VideoCapture", return_value=capture), \
                patch.object(self.engine.stop_event, "wait", return_value=False):
            self.engine._run("fake.mp4", "file", None, "manual", Path("fake.pt"), self.config, "pipeline")
        self.assertEqual(self.engine.snapshot()["status"], "completed")
        self.assertEqual(capture.retrieved, [0, 3, 6])
        self.assertEqual(frames, [0, 3, 6])
        self.assertEqual(self.engine.snapshot()["frame_index"], 6)
        self.assertAlmostEqual(self.engine.snapshot()["video_time_s"], 0.2)
        self.assertTrue(capture.released)

    def test_outstanding_capture_producer_blocks_new_session(self):
        release = threading.Event()
        producer = threading.Thread(target=release.wait)
        producer.start()
        self.engine.capture_thread = producer
        try:
            self.assertTrue(self.engine.active())
            with self.assertRaises(RuntimeError):
                self.engine.start({})
        finally:
            release.set()
            producer.join()
        self.assertFalse(self.engine.active())


if __name__ == "__main__":
    unittest.main()
