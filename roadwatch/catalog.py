"""One catalog interface for independently calibrated road-video datasets."""
from __future__ import annotations

from pathlib import Path
import threading

from .datasets import DatasetCatalog
from .comma_dataset import Comma2k19Catalog
from .comma_geometry import COMMA_CAMERA_PROVENANCE


class UnifiedCatalog:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.roadsens = DatasetCatalog(root)
        self.comma = Comma2k19Catalog(root)
        self.lock = threading.RLock()

    def refresh(self):
        with self.lock:
            self.comma.refresh()
            return self.recordings()

    def recordings(self):
        roadsens = [{**record, "dataset_id": "roadsens", "has_labels": record.get("annotation_path") is not None,
                     "calibration_available": False, "pose_source": "sparse_phone_gps"}
                    for record in self.roadsens.recordings()]
        return roadsens + self.comma.recordings()

    def recording(self, identity):
        return next((r for r in self.recordings() if r["id"] == str(identity)), None)

    def provider(self, identity):
        return self.comma if str(identity).startswith("comma2k19-") else self.roadsens

    def gps_track(self, identity):
        return self.provider(identity).gps_track(identity)

    def pose_at(self, identity, time_s, max_gap_s=3):
        return self.provider(identity).pose_at(identity, time_s, max_gap_s=max_gap_s)

    def reference_events(self, identity=None):
        if identity is None:
            return self.roadsens.reference_events()
        return self.provider(identity).reference_events(identity)

    def frame_time_s(self, identity, frame_index):
        if str(identity).startswith("comma2k19-"):
            return self.comma.frame_time_s(identity, frame_index)
        return None

    def calibration_for(self, identity):
        if str(identity).startswith("comma2k19-"):
            return self.comma.calibration_for(identity)
        return None

    def calibration_metadata(self, identity):
        if str(identity).startswith("comma2k19-"):
            record = self.recording(identity) or {}
            has_pose = bool(record.get("camera_pose_available"))
            return {**COMMA_CAMERA_PROVENANCE, "dataset_id": "comma2k19", "intrinsics_available": True,
                    "height_measured": False, "distortion_measured": False, "position_reference": "camera",
                    "camera_pose_available": has_pose,
                    "source": COMMA_CAMERA_PROVENANCE["intrinsics_source"],
                    "message": "Published camera matrix and synchronized camera pose; road height and zero distortion remain assumptions." if has_pose else "Published camera matrix; synchronized camera orientation is unavailable, so hazard localization is disabled."}
        return {"dataset_id": "roadsens", "intrinsics_available": False, "height_measured": False,
                "message": "No published RoadSens camera calibration was found; the saved manual profile applies."}

    def datasets(self):
        roadsens, comma = self.roadsens.summary(), self.comma.summary()
        return [{**roadsens, "id": "roadsens", "name": "RoadSens-4M", "unlabeled_recording_count": sum(r.get("annotation_path") is None for r in self.roadsens.recordings())},
                {**comma, "id": "comma2k19", "name": "comma2k19", "has_labels": False}]

    def summary(self):
        roadsens = self.roadsens.summary()
        comma = self.comma.summary()
        return {**roadsens,
                "recording_count": roadsens["recording_count"] + comma["recording_count"],
                "playable_recording_count": roadsens["playable_recording_count"] + comma["playable_recording_count"],
                "sensor_recording_count": roadsens["sensor_recording_count"] + comma.get("sensor_recording_count", comma["recording_count"]),
                "unlabeled_recording_count": comma["recording_count"],
                "issues": [*roadsens["issues"], *comma.get("issues", [])]}
