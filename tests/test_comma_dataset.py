"""Contracts for official comma2k19 clocks, layout, pose and label isolation."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from roadwatch.comma_dataset import Comma2k19Catalog


EARTH_A = 6_378_137.0
NORTH_QUATERNION = np.array([math.sqrt(0.5), 0.0, -math.sqrt(0.5), 0.0])
EAST_QUATERNION = np.array([0.5, -0.5, -0.5, 0.5])


def save_array(directory: Path, name: str, values, suffix: str = ""):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (name + suffix)).open("wb") as stream:
        np.save(stream, np.asarray(values))


def segment(root: Path, *, number="40", container="Example_1", pose_name="global_pose",
            suffix="", times=None, orientations=None):
    path = root / "datasets" / "comma2k19" / container / "route_2018-08-02--08-34-47" / number
    path.mkdir(parents=True, exist_ok=True)
    (path / "video.hevc").write_bytes(b"fixture elementary video stream")
    times = np.array([100.0, 100.1, 100.4]) if times is None else np.asarray(times)
    count = len(times)
    pose = path / pose_name
    positions = np.column_stack([np.full(count, EARTH_A), (times - times[0]) * 10, np.zeros(count)])
    orientations = np.tile(NORTH_QUATERNION, (count, 1)) if orientations is None else orientations
    save_array(pose, "frame_times", times, suffix)
    save_array(pose, "frame_positions", positions, suffix)
    save_array(pose, "frame_orientations", orientations, suffix)
    save_array(pose, "frame_velocities", np.tile([0.0, 0.0, 10.0], (count, 1)), suffix)
    return path


def recording(catalog):
    records = catalog.recordings()
    assert len(records) == 1
    return records[0]


@pytest.mark.parametrize("container,pose_name,suffix", [
    ("Example_1", "global_pose", ""),
    ("Dataset_chunk_1", "global_pos", ".npy"),
    ("Chunk_10", "global_pose", ""),
])
def test_official_sample_and_chunks_have_canonical_unlabeled_metadata(tmp_path, container, pose_name, suffix):
    path = segment(tmp_path, container=container, pose_name=pose_name, suffix=suffix)
    catalog = Comma2k19Catalog(tmp_path)
    item = recording(catalog)
    assert item["id"].startswith("comma2k19-")
    assert len(item["id"].split("-")[-1]) == 16
    assert item["video_path"] == str(path / "video.hevc")
    assert item["sensor_path"] == str(path / pose_name)
    assert item["annotation_path"] is None
    assert item["dataset_id"] == "comma2k19"
    assert item["has_labels"] is False
    assert item["camera_pose_available"] is True
    assert item["frame_count"] == 3
    assert item["fps"] == 20
    assert item["duration_s"] == pytest.approx(0.4)
    assert Comma2k19Catalog(tmp_path).recordings()[0]["id"] == item["id"]
    # Return values cannot mutate the cached catalog.
    item["label"] = "changed"
    assert "comma2k19" in catalog.recordings()[0]["label"]


def test_boot_clock_frame_times_override_nominal_twenty_fps(tmp_path):
    segment(tmp_path, times=[742.233, 742.281, 742.341])
    catalog = Comma2k19Catalog(tmp_path)
    identity = recording(catalog)["id"]
    assert catalog.frame_time_s(identity, 0) == 0
    assert catalog.frame_time_s(identity, 1) == pytest.approx(0.048)
    assert catalog.frame_time_s(identity, 2) == pytest.approx(0.108)
    for index in (-1, 3, True, 1.2, "1"):
        assert catalog.frame_time_s(identity, index) is None
    assert catalog.frame_time_s("unknown", 0) is None


def test_camera_pose_interpolates_ecef_and_shortest_quaternion_arc(tmp_path):
    segment(tmp_path, orientations=[NORTH_QUATERNION, NORTH_QUATERNION, -EAST_QUATERNION])
    catalog = Comma2k19Catalog(tmp_path)
    identity = recording(catalog)["id"]
    pose = catalog.pose_at(identity, 0.25)
    assert pose["ecef_position"] == pytest.approx([EARTH_A, 2.5, 0])
    assert np.linalg.norm(pose["ecef_orientation"]) == pytest.approx(1)
    assert pose["latitude"] == pytest.approx(0, abs=1e-9)
    assert pose["longitude"] == pytest.approx(math.degrees(math.atan2(2.5, EARTH_A)))
    assert pose["heading_deg"] == pytest.approx(45, abs=0.001)
    assert pose["speed_mps"] == pytest.approx(10)
    assert pose["accuracy_m"] is None
    assert pose["boot_time_s"] == pytest.approx(100.25)
    assert pose["time_s"] == pytest.approx(0.25)
    assert pose["position_reference"] == "camera"
    assert pose["orientation_convention"] == "camera_frd_to_ecef_hamilton_wxyz"
    assert catalog.pose_at(identity, 0)["heading_deg"] == pytest.approx(0)


def test_pose_never_extrapolates_or_bridges_long_telemetry_gaps(tmp_path):
    segment(tmp_path, times=[100, 100.1, 110])
    catalog = Comma2k19Catalog(tmp_path)
    identity = recording(catalog)["id"]
    assert catalog.pose_at(identity, 5) is None
    assert catalog.pose_at(identity, 5, max_gap_s=10) is not None
    assert catalog.pose_at(identity, 0.1, max_gap_s=0) is not None
    for query in (-0.01, 10.01, float("nan"), float("inf"), True, "1"):
        assert catalog.pose_at(identity, query) is None
    assert catalog.pose_at(identity, 0, max_gap_s=-1) is None
    assert catalog.pose_at("unknown", 0) is None


def test_map_is_bounded_and_retains_endpoints(tmp_path):
    segment(tmp_path, times=100 + np.arange(1601) / 20)
    catalog = Comma2k19Catalog(tmp_path)
    identity = recording(catalog)["id"]
    track = catalog.gps_track(identity)
    assert len(track) == 1200
    assert track[0]["time_s"] == 0
    assert track[-1]["time_s"] == 80
    assert track[-1]["longitude"] > track[0]["longitude"]
    assert all(point["accuracy_m"] is None for point in track)
    assert all("ecef_orientation" not in point for point in track)
    assert catalog.gps_track("unknown") == []


def test_published_calibration_preserves_provisional_height_and_distortion(tmp_path):
    segment(tmp_path)
    catalog = Comma2k19Catalog(tmp_path)
    identity = recording(catalog)["id"]
    profile = catalog.calibration_for(identity)
    assert (profile["width"], profile["height"]) == (1164, 874)
    assert [profile[key] for key in ("fx", "fy", "cx", "cy")] == [910, 910, 582, 437]
    assert profile["verified"] is False
    assert profile["distortion"] == [0] * 5
    assert profile["height_m"] == 1.4
    profile["distortion"][0] = 7
    assert catalog.calibration_for(identity)["distortion"][0] == 0
    assert catalog.calibration_for("unknown") is None


def test_annotations_are_never_inferred_from_other_datasets_or_nearby_files(tmp_path):
    path = segment(tmp_path)
    (path / "Annotation.csv").write_text("seconds_elapsed,text\n0.1,Pothole\n")
    catalog = Comma2k19Catalog(tmp_path)
    identity = recording(catalog)["id"]
    assert catalog.reference_events(identity) == []
    assert catalog.reference_events() == []
    summary = catalog.summary()
    assert summary["unlabeled_recording_count"] == 1
    assert summary["reference_event_count"] == 0
    assert summary["annotation_file_count"] == 0
    assert summary["hazard_counts"] == {}
    assert "predictions" in summary["label_note"]


def test_incomplete_downloads_invalid_arrays_and_empty_videos_are_not_published(tmp_path):
    segment(tmp_path, number="good")
    segment(tmp_path, number="half.partial")
    zero = segment(tmp_path, number="zero")
    (zero / "video.hevc").write_bytes(b"")
    partial = segment(tmp_path, number="partial")
    (partial / "video.hevc").rename(partial / "video.hevc.partial")
    missing = segment(tmp_path, number="missing")
    (missing / "global_pose" / "frame_times").unlink()
    corrupt = segment(tmp_path, number="bad")
    save_array(corrupt / "global_pose", "frame_orientations", np.zeros((3, 4)))
    unsorted = segment(tmp_path, number="unsorted", times=[100, 100.1, 100.05])
    object_array = segment(tmp_path, number="object")
    save_array(object_array / "global_pose", "frame_positions", np.array([{}], dtype=object))
    catalog = Comma2k19Catalog(tmp_path)
    assert recording(catalog)["segment_number"] == "good"
    assert any("incomplete or invalid" in issue for issue in catalog.summary()["issues"])


def test_refresh_discovers_new_complete_segments_and_cache_is_bounded(tmp_path, monkeypatch):
    import roadwatch.comma_dataset as module
    monkeypatch.setattr(module, "TELEMETRY_CACHE_LIMIT", 2)
    segment(tmp_path, number="1")
    catalog = Comma2k19Catalog(tmp_path)
    assert len(catalog.recordings()) == 1
    segment(tmp_path, number="2")
    segment(tmp_path, number="3")
    assert len(catalog.recordings()) == 1
    refreshed = catalog.refresh()
    assert len(refreshed) == 3
    assert len(catalog._telemetry) <= 2
    assert catalog.pose_at(refreshed[0]["id"], 0) is not None
    assert len(catalog._telemetry) <= 2
    segment(tmp_path, number="4")
    catalog.invalidate()
    assert len(catalog.recordings()) == 4


def test_example_and_full_release_share_identity_and_do_not_duplicate(tmp_path):
    example = segment(tmp_path, container="Example_1")
    catalog = Comma2k19Catalog(tmp_path)
    identity = recording(catalog)["id"]
    full = segment(tmp_path, container="Dataset_chunk_1")
    catalog.refresh()
    item = recording(catalog)
    assert item["id"] == identity
    assert item["segment_path"] == str(full)
    # A corrupt full-release copy cannot hide a working example.
    # Release read-only mappings before replacing source files on Windows.
    catalog.invalidate()
    (full / "global_pose" / "frame_times").unlink()
    catalog.refresh()
    assert recording(catalog)["segment_path"] == str(example)


def test_processed_gnss_fallback_uses_shared_boot_clock_and_circular_fields(tmp_path):
    path = segment(tmp_path, times=[100, 101, 102])
    (path / "global_pose" / "frame_orientations").unlink()
    gnss = path / "processed_log" / "GNSS" / "live_gnss_ublox"
    save_array(gnss, "t", [99.5, 100.5, 101.5, 102.5])
    save_array(gnss, "value", [
        [37, 179.8, 10, 1_530_000_000, 20, 359],
        [37, -179.8, 12, 1_530_000_001, 22, 1],
        [37, -179.6, 14, 1_530_000_002, 24, 3],
        [37, -179.4, 16, 1_530_000_003, 26, 5],
    ])
    catalog = Comma2k19Catalog(tmp_path)
    item = recording(catalog)
    assert item["camera_pose_available"] is False
    assert item["pose_source"] == "processed_gnss"
    assert item["sensor_path"] == str(gnss)
    pose = catalog.pose_at(item["id"], 0)
    assert pose["longitude"] == pytest.approx(-180)
    assert pose["heading_deg"] == pytest.approx(0)
    assert pose["speed_mps"] == pytest.approx(11)
    assert pose["accuracy_m"] is None
    assert "ecef_orientation" not in pose
    assert any("fallback" in issue for issue in catalog.summary()["issues"])
    assert catalog.pose_at(item["id"], -0.1) is None
    assert catalog.pose_at(item["id"], 2.1) is None


def test_missing_dataset_is_empty_without_hiding_roadsens_references(tmp_path):
    catalog = Comma2k19Catalog(tmp_path)
    assert catalog.recordings() == []
    assert catalog.summary()["recording_count"] == 0
    assert catalog.reference_events("34") == []


def test_downloaded_official_sample_has_synchronized_real_camera_poses():
    from roadwatch.geometry import haversine_m
    root = Path(__file__).resolve().parents[1]
    sample = root / "datasets" / "comma2k19" / "Example_1" / "b0c9d2329ad1606b_2018-08-02--08-34-47" / "40"
    if not (sample / "global_pose" / "frame_times").is_file():
        pytest.skip("Official sample has not been downloaded")
    catalog = Comma2k19Catalog(root)
    matches = [item for item in catalog.recordings() if item["route_id"] == sample.parent.name and item["segment_number"] == "40"]
    assert len(matches) == 1
    item = matches[0]
    assert item["frame_count"] == 1200
    assert item["duration_s"] == pytest.approx(59.94916, abs=1e-5)
    points = catalog.gps_track(item["id"])
    assert len(points) == 1200
    assert points[0]["latitude"] == pytest.approx(37.721000009, abs=1e-8)
    assert points[0]["longitude"] == pytest.approx(-122.472299089, abs=1e-8)
    pose = catalog.pose_at(item["id"], 0)
    times = np.load(sample / "global_pose" / "frame_times", allow_pickle=False)
    gnss = sample / "processed_log" / "GNSS" / "live_gnss_ublox"
    timestamps = np.load(gnss / "t", allow_pickle=False).reshape(-1)
    values = np.load(gnss / "value", allow_pickle=False)
    nearest = int(np.argmin(np.abs(timestamps - times[0])))
    # This checks global position and source clocks against a separate native
    # log, rather than asserting that the adapter agrees with its own output.
    assert abs(float(timestamps[nearest] - times[0])) < 0.2
    assert haversine_m(pose["latitude"], pose["longitude"], float(values[nearest, 0]), float(values[nearest, 1])) < 2
