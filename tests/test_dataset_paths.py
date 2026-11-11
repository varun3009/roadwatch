"""Segment discovery must avoid log trees and incomplete downloads."""
from pathlib import Path

from roadwatch.dataset_paths import iter_segment_videos


def video(root: Path, path: str, contents=b"video") -> Path:
    result = root / path
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_bytes(contents)
    return result


def test_discovers_arbitrarily_nested_segments_and_all_supported_video_types(tmp_path):
    expected = {
        video(tmp_path, "Chunk_1/route/40/video.hevc"),
        video(tmp_path, "Chunk_1/route/40/video.mp4"),
        video(tmp_path, "archive/version/extra/more/chunk/route/41/VIDEO.H265"),
        video(tmp_path, "Example_1/another_route/6/video.mkv"),
    }
    assert set(iter_segment_videos(tmp_path)) == expected


def test_telemetry_temporary_and_child_segments_are_not_discovered(tmp_path):
    expected = video(tmp_path, "Chunk_1/route/40/video.hevc")
    for path in (
        "Chunk_1/route/40/nested_fake_segment/video.hevc",
        "processed_log/GNSS/fake/video.mp4",
        "global_pose/fake/video.hevc",
        "global_pos/fake/video.hevc",
        "Chunk_1/_extracting/route/41/video.hevc",
        "Chunk_1/half.partial/route/41/video.hevc",
        "Chunk_1/temp/route/41/video.hevc",
        "Chunk_1/TMP/route/41/video.hevc",
        "Chunk_1/.downloads/route/41/video.hevc",
        "Chunk_1/route/41/video.hevc.partial",
        "Chunk_1/route/42/video.avi",
    ):
        video(tmp_path, path)
    video(tmp_path, "Chunk_1/route/43/video.hevc", contents=b"")
    assert list(iter_segment_videos(tmp_path)) == [expected]


def test_pruned_log_directory_is_never_enumerated(tmp_path, monkeypatch):
    import os
    original_scandir = os.scandir
    expected = video(tmp_path, "arbitrary/route/40/video.hevc")
    blocked = tmp_path / "processed_log"
    video(tmp_path, "processed_log/thousands_of_files/fake/video.hevc")

    def guarded_scandir(path):
        # Inspect the costly filesystem boundary, not os.walk's implementation:
        # filtering results after a recursive walk would still fail this check.
        assert Path(path) != blocked, "Telemetry directory was unnecessarily enumerated"
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)
    assert list(iter_segment_videos(tmp_path)) == [expected]


def test_missing_root_is_empty(tmp_path):
    assert list(iter_segment_videos(tmp_path / "not-downloaded")) == []
