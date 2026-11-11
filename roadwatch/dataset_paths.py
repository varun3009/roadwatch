"""Cheap segment discovery without walking high-rate telemetry subtrees."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator


VIDEO_SUFFIXES = frozenset({".hevc", ".h265", ".mp4", ".mkv"})
_PRUNED_DIRECTORIES = frozenset({
    "processed_log", "global_pose", "global_pos", "temp", "tmp", ".downloads", ".download",
})
_INCOMPLETE_SUFFIXES = (".partial", ".tmp", ".temp", ".download")


def _skip_directory(name: str) -> bool:
    normalized = name.casefold()
    return (
        normalized.startswith("_")
        or normalized in _PRUNED_DIRECTORIES
        or normalized.endswith(_INCOMPLETE_SUFFIXES)
    )


def iter_segment_videos(data_root: Path) -> Iterator[Path]:
    """Yield complete video paths at any nesting depth, pruning segment children.

    NumPy/processed-log directories are not candidate segments. Walking their
    thousands of sensor entries made catalog refresh and downloader ready counts
    expensive on hard disks. ``os.walk`` exposes its pending child list so these
    trees are pruned before their entries are enumerated. A discovered segment
    is a leaf even when it contains additional nested directories.
    """
    root = Path(data_root)
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        directories[:] = [name for name in directories if not _skip_directory(name)]
        videos = []
        for name in filenames:
            normalized = name.casefold()
            if not normalized.startswith("video.") or Path(name).suffix.casefold() not in VIDEO_SUFFIXES:
                continue
            path = Path(current) / name
            try:
                if not path.is_symlink() and path.is_file() and path.stat().st_size > 0:
                    videos.append(path)
            except OSError:
                # An atomic download publication/removal can race discovery.
                continue
        if videos:
            directories[:] = []
            yield from videos
