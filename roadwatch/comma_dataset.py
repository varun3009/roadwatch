"""Unlabelled comma2k19 video and synchronized camera-pose catalog.

Native NumPy files deliberately have no ``.npy`` suffix. All clocks are kept in
device boot seconds until a video's first frame time is subtracted. Hazard
references are never inferred from GPS, CAN, or the existing RoadSens labels.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass
import bisect
import hashlib
import math
from pathlib import Path
import threading
from typing import Any

import numpy as np

from .comma_geometry import (
    comma_camera_calibration,
    ecef_pose_to_geodetic,
    quaternion_slerp,
)
from .dataset_paths import iter_segment_videos


DATASET_ID = "comma2k19"
DATASET_URL = "https://github.com/commaai/comma2k19"
MAP_POINT_LIMIT = 1200
TELEMETRY_CACHE_LIMIT = 32


def _number(value: Any) -> float | None:
    if isinstance(value, (bool, str, bytes)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _numpy_path(directory: Path, name: str) -> Path | None:
    for path in (directory / name, directory / (name + ".npy")):
        if path.is_file():
            return path
    return None


def _read_numpy(directory: Path, name: str) -> np.ndarray:
    path = _numpy_path(directory, name)
    if path is None:
        raise ValueError(f"Missing {directory.name}/{name}")
    array = np.load(path, allow_pickle=False, mmap_mode="r")
    if not isinstance(array, np.ndarray) or array.dtype.kind not in "fiu":
        raise ValueError(f"{path.name} must be a numeric NumPy array")
    return array


def _times(array: np.ndarray, name: str) -> np.ndarray:
    if array.ndim == 2 and array.shape[1] == 1:
        array = array[:, 0]
    if array.ndim != 1 or not 1 <= len(array) <= 2_000_000:
        raise ValueError(f"{name} must contain a nonempty timestamp vector")
    if not np.isfinite(array).all() or np.any(array[1:] <= array[:-1]):
        raise ValueError(f"{name} must be finite and strictly increasing")
    return array


def _matrix(array: np.ndarray, rows: int, columns: int, name: str) -> np.ndarray:
    if array.shape != (rows, columns) or not np.isfinite(array).all():
        raise ValueError(f"{name} must be a finite {rows} x {columns} array")
    return array


@dataclass
class _Telemetry:
    frame_times: np.ndarray
    sensor_path: Path
    positions: np.ndarray | None = None
    orientations: np.ndarray | None = None
    velocities: np.ndarray | None = None
    gnss_times: np.ndarray | None = None
    gnss_values: np.ndarray | None = None

    @property
    def pose_source(self) -> str:
        return "global_pose" if self.positions is not None else "processed_gnss"


class Comma2k19Catalog:
    """Read extracted official segments below ``root/datasets/comma2k19``.

    ``root`` is the project directory, not a chunk directory. Full chunks and
    the official Example_1 layout can coexist. Downloads should publish each
    file atomically from a ``.partial`` path, then call :meth:`refresh`.
    Metadata is cached until refresh; at most 32 segments' arrays remain mapped.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.data_root = self.root / "datasets" / DATASET_ID
        self._recordings: list[dict] | None = None
        self._by_id: dict[str, dict] = {}
        self._telemetry: OrderedDict[str, _Telemetry] = OrderedDict()
        self._issues: set[str] = set()
        self._lock = threading.RLock()

    def refresh(self) -> list[dict]:
        """Invalidate completed-download metadata and discover current files."""
        with self._lock:
            self._recordings = None
            self._by_id.clear()
            self._telemetry.clear()
            self._issues.clear()
            return self.recordings()

    def invalidate(self) -> None:
        with self._lock:
            self._recordings = None
            self._by_id.clear()
            self._telemetry.clear()
            self._issues.clear()

    def _identity(self, segment: Path) -> str:
        # Chunks are packaging, not identity. The official example also occurs
        # in the full release; importing it must not duplicate replay or events.
        # Treat the official NTFS pipe replacement as the same logical route.
        relative = (segment.parent.name + "/" + segment.name).replace("|", "_")
        return DATASET_ID + "-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]

    def _read_telemetry(self, segment: Path) -> _Telemetry:
        pose_directory = next(
            (path for path in (segment / "global_pose", segment / "global_pos")
             if path.is_dir() and _numpy_path(path, "frame_times") is not None),
            None,
        )
        if pose_directory is None:
            raise ValueError("Missing global_pose/frame_times; video/GNSS alignment is unavailable")
        frame_times = _times(_read_numpy(pose_directory, "frame_times"), "frame_times")
        try:
            count = len(frame_times)
            positions = _matrix(_read_numpy(pose_directory, "frame_positions"), count, 3, "frame_positions")
            orientations = _matrix(_read_numpy(pose_directory, "frame_orientations"), count, 4, "frame_orientations")
            norms = np.linalg.norm(orientations, axis=1)
            if np.any((norms < 0.99) | (norms > 1.01)):
                raise ValueError("frame_orientations must contain near-unit quaternions")
            radii = np.linalg.norm(positions, axis=1)
            if np.any((radii < 1_000_000) | (radii > 100_000_000)):
                raise ValueError("frame_positions contains an implausible ECEF position")
            velocities = None
            if _numpy_path(pose_directory, "frame_velocities") is not None:
                velocities = _matrix(_read_numpy(pose_directory, "frame_velocities"), count, 3, "frame_velocities")
            return _Telemetry(frame_times, pose_directory, positions, orientations, velocities)
        except (OSError, ValueError, EOFError) as pose_error:
            # The GNSS log shares the boot clock, but contains no camera attitude.
            for source in ("live_gnss_ublox", "live_gnss_qcom"):
                directory = segment / "processed_log" / "GNSS" / source
                if not directory.is_dir():
                    continue
                try:
                    times = _read_numpy(directory, "t")
                    if times.ndim == 2 and times.shape[1] == 1:
                        times = times[:, 0]
                    values = _read_numpy(directory, "value")
                    if times.ndim != 1 or values.ndim != 2 or len(values) != len(times) or values.shape[1] < 6:
                        raise ValueError("GNSS timestamps and six-column values are inconsistent")
                    valid = (
                        np.isfinite(times) & np.isfinite(values[:, :6]).all(axis=1)
                        & (np.abs(values[:, 0]) <= 90) & (np.abs(values[:, 1]) <= 180)
                        & (values[:, 2] >= 0)
                    )
                    times, values = np.asarray(times[valid]), np.asarray(values[valid, :6])
                    order = np.argsort(times, kind="stable")
                    times, values = times[order], values[order]
                    unique = np.r_[True, np.diff(times) > 0] if len(times) else np.array([], dtype=bool)
                    times, values = times[unique], values[unique]
                    _times(times, "GNSS times")
                    if times[-1] < frame_times[0] or times[0] > frame_times[-1]:
                        raise ValueError("GNSS timestamps do not overlap the video")
                    self._issues.add(f"{segment.relative_to(self.data_root)} uses {source} fallback: {pose_error}. Camera attitude is unavailable.")
                    return _Telemetry(frame_times, directory, gnss_times=times, gnss_values=values)
                except (OSError, ValueError, EOFError) as exc:
                    self._issues.add(f"Cannot read {directory.relative_to(self.data_root)}: {exc}")
            raise ValueError(f"Camera pose unavailable and no synchronized processed GNSS fallback: {pose_error}") from pose_error

    def _cache(self, identity: str, telemetry: _Telemetry) -> None:
        self._telemetry[identity] = telemetry
        self._telemetry.move_to_end(identity)
        while len(self._telemetry) > TELEMETRY_CACHE_LIMIT:
            self._telemetry.popitem(last=False)

    def recordings(self) -> list[dict]:
        with self._lock:
            if self._recordings is not None:
                return [dict(item) for item in self._recordings]
            self._recordings = []
            if not self.data_root.is_dir():
                return []
            candidates: dict[Path, list[Path]] = {}
            for video in iter_segment_videos(self.data_root):
                try:
                    if not video.resolve().is_relative_to(self.data_root.resolve()) or not video.is_file() or video.stat().st_size == 0:
                        continue
                except OSError:
                    continue
                candidates.setdefault(video.parent, []).append(video)
            # Prefer a completed full-release copy over the example, while
            # preserving IDs when the background download replaces a sample.
            for segment, videos in sorted(candidates.items(), key=lambda item: (
                any(part.casefold().startswith("example") for part in item[0].relative_to(self.data_root).parts),
                item[0].as_posix(),
            )):
                identity = self._identity(segment)
                if identity in self._by_id:
                    continue
                try:
                    telemetry = self._read_telemetry(segment)
                except (OSError, ValueError, EOFError) as exc:
                    self._issues.add(f"Skipped incomplete or invalid segment {segment.relative_to(self.data_root)}: {exc}")
                    continue
                # A frame-preserving MP4 transcode may be provided for decoders
                # that cannot seek the raw HEVC elementary stream reliably.
                video = min(videos, key=lambda path: (path.suffix.casefold() != ".mp4", path.name))
                route, number = segment.parent.name.replace("|", "_"), segment.name
                record = {
                    "id": identity, "label": f"comma2k19 · {route} · segment {number}",
                    "video_path": str(video), "sensor_path": str(telemetry.sensor_path),
                    "annotation_path": None, "dataset_id": DATASET_ID,
                    "has_labels": False, "calibration_available": True,
                    "camera_pose_available": telemetry.positions is not None,
                    "pose_source": telemetry.pose_source,
                    "pose_mode": "camera_ecef" if telemetry.positions is not None else "gnss_fallback",
                    "frame_count": len(telemetry.frame_times), "fps": 20.0,
                    "duration_s": float(telemetry.frame_times[-1] - telemetry.frame_times[0]),
                    "segment_path": str(segment), "route_id": route, "segment_number": number,
                }
                self._recordings.append(record)
                self._by_id[identity] = record
                self._cache(identity, telemetry)
            return [dict(item) for item in self._recordings]

    def _load(self, recording_id: str) -> _Telemetry | None:
        with self._lock:
            self.recordings()
            if recording_id not in self._by_id:
                return None
            if recording_id in self._telemetry:
                self._telemetry.move_to_end(recording_id)
                return self._telemetry[recording_id]
            try:
                telemetry = self._read_telemetry(Path(self._by_id[recording_id]["segment_path"]))
            except (OSError, ValueError, EOFError) as exc:
                self._issues.add(f"Cannot reload {recording_id}: {exc}")
                return None
            self._cache(recording_id, telemetry)
            return telemetry

    def frame_time_s(self, recording_id: str, frame_index: int) -> float | None:
        """Return exact source-frame time relative to the first video frame."""
        if isinstance(frame_index, bool) or not isinstance(frame_index, (int, np.integer)):
            return None
        telemetry = self._load(str(recording_id))
        if telemetry is None or not 0 <= frame_index < len(telemetry.frame_times):
            return None
        return float(telemetry.frame_times[frame_index] - telemetry.frame_times[0])

    def _pose(self, telemetry: _Telemetry, query: float, max_gap_s: float) -> dict | None:
        boot_time = float(telemetry.frame_times[0]) + query
        # GNSS samples outside the video range must not enable extrapolation of
        # the video itself, including accidental negative replay time offsets.
        if boot_time < telemetry.frame_times[0] or boot_time > telemetry.frame_times[-1]:
            return None
        times = telemetry.frame_times if telemetry.positions is not None else telemetry.gnss_times
        assert times is not None
        index = bisect.bisect_left(times, boot_time)
        if index < len(times) and abs(float(times[index]) - boot_time) <= 1e-9:
            before = after = index
            fraction = 0.0
        else:
            if index == 0 or index == len(times):
                return None
            before, after = index - 1, index
            gap = float(times[after] - times[before])
            if gap <= 0 or gap > max_gap_s:
                return None
            fraction = (boot_time - float(times[before])) / gap
        if telemetry.positions is not None:
            assert telemetry.orientations is not None
            position_before = np.asarray(telemetry.positions[before], dtype=np.float64)
            position_after = np.asarray(telemetry.positions[after], dtype=np.float64)
            position = position_before + fraction * (position_after - position_before)
            velocity = None
            if telemetry.velocities is not None:
                velocity_before = np.asarray(telemetry.velocities[before], dtype=np.float64)
                velocity_after = np.asarray(telemetry.velocities[after], dtype=np.float64)
                velocity = velocity_before + fraction * (velocity_after - velocity_before)
            try:
                orientation = quaternion_slerp(telemetry.orientations[before], telemetry.orientations[after], fraction)
                result = ecef_pose_to_geodetic(position, orientation, velocity)
            except ValueError:
                return None
            result.update(
                ecef_position=np.asarray(position).tolist(),
                ecef_orientation=np.asarray(orientation).tolist(),
                ecef_velocity=np.asarray(velocity).tolist() if velocity is not None else None,
            )
        else:
            assert telemetry.gnss_values is not None
            left = np.asarray(telemetry.gnss_values[before], dtype=np.float64)
            right = np.asarray(telemetry.gnss_values[after], dtype=np.float64)
            result = {
                "latitude": float(left[0] + (right[0] - left[0]) * fraction),
                "longitude": float(((left[1] + ((right[1] - left[1] + 180) % 360 - 180) * fraction + 180) % 360) - 180),
                "speed_mps": float(left[2] + (right[2] - left[2]) * fraction),
                "altitude_m": float(left[4] + (right[4] - left[4]) * fraction),
                "heading_deg": float((left[5] + ((right[5] - left[5] + 180) % 360 - 180) * fraction) % 360),
            }
        result.update(
            time_s=query, boot_time_s=boot_time, accuracy_m=None,
            pose_source=telemetry.pose_source, dataset_id=DATASET_ID,
            pose_mode="camera_ecef" if telemetry.positions is not None else "gnss_fallback",
            accuracy_note="Dataset does not provide per-frame horizontal accuracy; total hazard uncertainty is unmeasured.",
        )
        return result

    def pose_at(self, recording_id: str, time_s: float, max_gap_s: float = 3) -> dict | None:
        """Interpolate ECEF positions and quaternion attitudes without long gaps."""
        query, gap_limit = _number(time_s), _number(max_gap_s)
        if query is None or query < 0 or gap_limit is None or gap_limit < 0:
            return None
        telemetry = self._load(str(recording_id))
        return self._pose(telemetry, query, gap_limit) if telemetry is not None else None

    def gps_track(self, recording_id: str) -> list[dict]:
        """Bound map payloads while retaining the first and last source poses."""
        telemetry = self._load(str(recording_id))
        if telemetry is None:
            return []
        times = telemetry.frame_times if telemetry.positions is not None else telemetry.gnss_times
        assert times is not None
        indices = np.unique(np.linspace(0, len(times) - 1, min(MAP_POINT_LIMIT, len(times)), dtype=int))
        result = []
        for index in indices:
            point = self._pose(telemetry, float(times[index] - telemetry.frame_times[0]), 3.0)
            if point is not None:
                # Native ECEF/quaternion payload is needed by projection, not
                # every background map point. Keep that endpoint lightweight.
                result.append({key: point.get(key) for key in (
                    "time_s", "latitude", "longitude", "altitude_m", "heading_deg",
                    "speed_mps", "accuracy_m", "pose_source", "dataset_id",
                )})
        return result

    def calibration_for(self, recording_id: str) -> dict | None:
        self.recordings()
        return asdict(comma_camera_calibration()) if str(recording_id) in self._by_id else None

    def reference_events(self, recording_id: str | None = None) -> list[dict]:
        """No ground-truth road-hazard annotations ship with comma2k19."""
        return []

    def summary(self) -> dict:
        recordings = self.recordings()
        return {
            "id": DATASET_ID, "dataset_id": DATASET_ID, "name": "comma2k19",
            "root": str(self.data_root), "recording_count": len(recordings),
            "playable_recording_count": len(recordings),
            "sensor_recording_count": len(recordings),
            "unlabeled_recording_count": len(recordings), "has_labels": False,
            "annotation_file_count": 0, "reference_event_count": 0,
            "hazard_counts": {}, "workbook_count": 0, "workbook_paths": [],
            "camera_pose_recording_count": sum(item["camera_pose_available"] for item in recordings),
            "reference_location_kind": "unavailable", "sources": [DATASET_URL],
            "issues": sorted(self._issues),
            "label_note": "Unlabeled inference dataset. Model detections are predictions, not verified hazard labels.",
        }
