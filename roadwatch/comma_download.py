"""Resumable download of the official comma2k19 sample and full release.

The worker runs separately from the web server so a UI restart does not cancel
large transfers. Files are checked against publisher hashes before extraction.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote
import zipfile
import zlib

import httpx

from .dataset_paths import iter_segment_videos

GITHUB_REPO = "https://api.github.com/repos/commaai/comma2k19"
GITHUB_RAW = "https://raw.githubusercontent.com/commaai/comma2k19/master/"
HF_REPO = "https://huggingface.co/api/datasets/commaai/comma2k19"
HF_RAW = "https://huggingface.co/datasets/commaai/comma2k19/resolve/main/"
ROOT = Path(__file__).resolve().parents[1]


class DownloadPaused(Exception):
    pass


def paths(root=ROOT):
    root = Path(root)
    runtime = root / "roadwatch_data"
    runtime.mkdir(parents=True, exist_ok=True)
    destination = root / "datasets" / "comma2k19"
    destination.mkdir(parents=True, exist_ok=True)
    return destination, runtime / "comma_download_status.json", runtime / "comma_download_pause"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, allow_nan=False)
        for attempt in range(8):
            try:
                Path(temporary).replace(path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(0.01 * 2 ** attempt, 0.3))
    finally:
        Path(temporary).unlink(missing_ok=True)


def download_status(root=ROOT):
    _, status_path, _ = paths(root)
    try:
        return json.loads(status_path.read_text("utf-8"))
    except (OSError, ValueError):
        return {"status": "idle", "message": "comma2k19 has not been downloaded yet.", "bytes_downloaded": 0,
                "bytes_total": 0, "files_completed": 0, "files_total": 0, "segments_ready": 0}


def pause_download(root=ROOT):
    _, _, pause = paths(root)
    pause.write_text("Pause requested", encoding="utf-8")
    return {**download_status(root), "message": "Pausing after the current network block; partial files are retained."}


def start_download(root=ROOT, scope="all", workers=3):
    if scope not in ("all", "sample", "full"):
        raise ValueError("Download scope must be sample, full, or all")
    destination, status_path, pause = paths(root)
    current = download_status(root)
    if current.get("status") in ("starting", "downloading", "verifying", "extracting") and time.time() - current.get("updated_at", 0) < 30:
        return current
    pause.unlink(missing_ok=True)
    runtime = status_path.parent
    command = [sys.executable, "-u", str(Path(root) / "scripts" / "download_comma2k19.py"),
               "--scope", scope, "--workers", str(workers)]
    current = {"status": "starting", "message": "Connecting to the official comma.ai dataset releases…", "pid": None,
               "scope": scope, "updated_at": time.time(), "bytes_downloaded": current.get("bytes_downloaded", 0),
               "bytes_total": current.get("bytes_total", 0), "files_completed": current.get("files_completed", 0),
               "files_total": current.get("files_total", 0), "segments_ready": current.get("segments_ready", 0)}
    atomic_json(status_path, current)
    try:
        with (runtime / "comma_download.log").open("a", encoding="utf-8") as log:
            options = {"cwd": str(root), "stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                options["start_new_session"] = True
            process = subprocess.Popen(command, **options)
    except OSError as exc:
        atomic_json(status_path, {**current, "status": "error", "message": str(exc)})
        raise
    # The worker owns subsequent status writes. Do not overwrite its newer state.
    return {**download_status(root), "pid": process.pid}


def safe_target(destination: Path, relative: str) -> Path:
    """Normalize published Unix route names for NTFS and reject traversal."""
    relative = relative.replace("\\", "/")
    entry = PurePosixPath(relative)
    if entry.is_absolute() or not entry.parts or any(part in ("..", ".") for part in entry.parts):
        raise ValueError(f"Unsafe archive path: {relative}")
    if re.match(r"^[a-zA-Z]:", relative):
        raise ValueError("Archive path must be relative")
    components = []
    for part in entry.parts:
        clean = re.sub(r'[<>:"|?*\x00-\x1f]', "_", part).rstrip(" .")
        if not clean:
            raise ValueError("Archive path has an empty normalized component")
        if clean.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            clean = "_" + clean
        components.append(clean)
    target = destination.joinpath(*components).resolve()
    if not target.is_relative_to(destination.resolve()):
        raise ValueError("Archive target escapes the dataset directory")
    return target


@contextmanager
def worker_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError("A comma2k19 download worker is already active") from None
    try:
        yield
    finally:
        stream.close()


class Downloader:
    def __init__(self, root=ROOT, workers=3):
        self.root = Path(root)
        self.destination, self.status_path, self.pause_path = paths(root)
        self.workers = max(1, min(4, int(workers)))
        self.lock = threading.RLock()
        self.abort = threading.Event()
        self.state = {"status": "starting", "pid": os.getpid(), "updated_at": time.time(), "files_total": 0,
                      "files_completed": 0, "bytes_total": 0, "bytes_downloaded": 0, "bytes_per_second": 0,
                      "segments_ready": 0, "current_file": "", "message": "Preparing download…", "sources": [GITHUB_REPO, HF_REPO]}
        self.progress = {}
        self.last_write = 0
        self.rate_start = time.monotonic()
        self.received_bytes = 0
        self.last_segment_scan = 0

    def check_pause(self):
        if self.pause_path.exists() or self.abort.is_set():
            raise DownloadPaused()

    def update(self, force=False, **changes):
        with self.lock:
            self.state.update(changes)
            self.state["bytes_downloaded"] = sum(self.progress.values())
            self.state["bytes_per_second"] = round(self.received_bytes / max(1, time.monotonic() - self.rate_start))
            self.state["updated_at"] = time.time()
            if force or time.monotonic() - self.last_write > 1:
                self.last_write = time.monotonic()
                atomic_json(self.status_path, self.state)

    def _hash(self, path, github=False):
        digest = hashlib.sha1() if github else hashlib.sha256()
        if github:
            digest.update(f"blob {path.stat().st_size}\0".encode())
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                self.check_pause()
                digest.update(block)
                self.update()
        return digest.hexdigest()

    def fetch(self, item):
        self.check_pause()
        target = safe_target(self.destination, item["local"])
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(str(target) + ".partial")
        key, size = item["local"], item["size"]
        if target.is_file() and target.stat().st_size == size:
            if self._hash(target, item.get("github", False)) == item["hash"]:
                with self.lock:
                    self.progress[key] = size
                    self.state["files_completed"] += 1
                self.update()
                return target
            raise ValueError(f"Existing file failed publisher checksum: {target.name}")
        for attempt in range(6):
            self.check_pause()
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > size:
                raise ValueError(f"Partial file exceeds published size: {target.name}")
            with self.lock:
                self.progress[key] = offset
            if offset < size:
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                try:
                    # A fresh resolver URL avoids stale signed download redirects on retries.
                    url = item["url"] + ("&" if "?" in item["url"] else "?") + f"download=true&roadwatch={int(time.time())}"
                    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, connect=30)) as client:
                        with client.stream("GET", url, headers=headers) as response:
                            response.raise_for_status()
                            if offset and response.status_code != 206:
                                raise ValueError("Server did not honor the resume range; retained partial file")
                            if response.status_code == 206 and not response.headers.get("content-range", "").startswith(f"bytes {offset}-"):
                                raise ValueError("Server returned a mismatched resume range")
                            self.update(status="downloading", current_file=target.name, message=f"Downloading {target.name}")
                            with partial.open("ab" if offset else "wb") as output:
                                for block in response.iter_bytes(1024 * 1024):
                                    self.check_pause()
                                    output.write(block)
                                    offset += len(block)
                                    if offset > size:
                                        raise ValueError("Download exceeds publisher file size")
                                    with self.lock:
                                        self.progress[key] = offset
                                        self.received_bytes += len(block)
                                    self.update()
                    if offset != size:
                        raise IOError(f"Incomplete transfer: {offset} of {size} bytes")
                except (httpx.HTTPError, OSError) as exc:
                    if attempt == 5:
                        raise
                    self.update(message=f"Retrying {target.name}: {type(exc).__name__}")
                    self.abort.wait(min(2 ** attempt, 20))
                    continue
            self.update(status="verifying", current_file=target.name, message=f"Checking publisher hash for {target.name}")
            if self._hash(partial, item.get("github", False)) != item["hash"]:
                raise ValueError(f"Publisher checksum failed: {target.name}; partial file retained for inspection")
            partial.replace(target)
            with self.lock:
                self.state["files_completed"] += 1
            self.update(force=True)
            return target
        raise IOError(f"Could not download {target.name}")

    def sample_manifest(self):
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(GITHUB_REPO + "/git/trees/master?recursive=1")
            response.raise_for_status()
            entries = response.json()["tree"]
        manifest = []
        for entry in entries:
            remote = entry["path"]
            if entry["type"] != "blob" or not (remote.startswith("Example_1/") or remote in ("notebooks/camera_intrinsics.txt", "LICENSE", "README.md")):
                continue
            local = remote if remote.startswith("Example_1/") else "_source/" + remote
            manifest.append({"url": GITHUB_RAW + quote(remote, safe="/"), "local": local,
                             "size": entry["size"], "hash": entry["sha"], "github": True})
        return manifest

    def full_manifest(self):
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(HF_REPO + "/tree/main/raw_data")
            response.raise_for_status()
            entries = response.json()
        items = [{"url": HF_RAW + quote(e["path"], safe="/"), "local": "_archives/" + Path(e["path"]).name,
                  "size": e["size"], "hash": e["lfs"]["oid"], "github": False}
                 for e in entries if re.fullmatch(r"raw_data/Chunk_\d+\.zip", e.get("path", ""))]
        if len(items) != 10:
            raise ValueError("Official full release does not contain the expected ten chunks")
        return sorted(items, key=lambda item: int(re.search(r"Chunk_(\d+)", item["local"])[1]))

    def extract(self, archive):
        marker = Path(str(archive) + ".extracted.json")
        with zipfile.ZipFile(archive) as compressed:
            entries = compressed.infolist()
            if marker.exists():
                complete = True
                for item in entries:
                    self.check_pause()
                    target = safe_target(self.destination, item.filename)
                    if not item.is_dir() and (not target.is_file() or target.stat().st_size != item.file_size):
                        complete = False
                        break
                if complete:
                    return
            self.update(status="extracting", current_file=archive.name, message=f"Extracting {archive.name}; adapting Unix route names for Windows")
            targets = set()
            needed = sum(item.file_size for item in entries if not item.is_dir())
            if shutil.disk_usage(self.destination).free < needed + 2 * 1024 ** 3:
                raise OSError("Insufficient free disk space to extract this chunk")
            for item in entries:
                self.check_pause()
                mode = item.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    raise ValueError("Dataset archive contains a symbolic link")
                target = safe_target(self.destination, item.filename)
                if target in targets:
                    raise ValueError("Two archive entries normalize to the same Windows path")
                targets.add(target)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_file() and target.stat().st_size == item.file_size:
                    crc = 0
                    with target.open("rb") as existing:
                        while block := existing.read(1024 * 1024):
                            self.check_pause()
                            crc = zlib.crc32(block, crc)
                            self.update()
                    if crc & 0xFFFFFFFF == item.CRC:
                        continue
                partial = Path(str(target) + ".partial")
                with compressed.open(item) as source, partial.open("wb") as output:
                    while block := source.read(1024 * 1024):
                        self.check_pause()
                        output.write(block)
                        self.update()
                partial.replace(target)
                if (target.name.removesuffix(".npy") in {"video.hevc", "video.mp4", "video.h265", "video.mkv", "frame_times", "frame_positions", "frame_orientations"}
                        and time.monotonic() - self.last_segment_scan > 5):
                    self._count_segments()
            atomic_json(marker, {"archive": archive.name, "entries": len(entries), "extracted_at": time.time()})
        self._count_segments()

    def _count_segments(self):
        identities = set()
        for video in iter_segment_videos(self.destination):
            segment = video.parent
            if any(all(any((segment / directory / (name + suffix)).is_file() for suffix in ("", ".npy"))
                       for name in ("frame_times", "frame_positions", "frame_orientations"))
                   for directory in ("global_pose", "global_pos")):
                identities.add((segment.parent.name.replace("|", "_"), segment.name))
        self.last_segment_scan = time.monotonic()
        self.update(force=True, segments_ready=len(identities))

    def run(self, scope="all"):
        self.pause_path.unlink(missing_ok=True)
        self.state["scope"] = scope
        with worker_lock(self.status_path.with_suffix(".lock")):
            try:
                sample = self.sample_manifest() if scope in ("sample", "all") else []
                full = self.full_manifest() if scope in ("full", "all") else []
                items = sample + full
                self.state.update(files_total=len(items), bytes_total=sum(item["size"] for item in items))
                for item in items:
                    target = safe_target(self.destination, item["local"])
                    partial = Path(str(target) + ".partial")
                    self.progress[item["local"]] = min(item["size"], target.stat().st_size if target.exists() else partial.stat().st_size if partial.exists() else 0)
                manifest_path = self.destination / "_source" / "download_manifest.json"
                try:
                    previous = json.loads(manifest_path.read_text("utf-8"))["files"]
                except (OSError, ValueError, KeyError):
                    previous = []
                merged = {item["local"]: item for item in [*previous, *items]}
                atomic_json(manifest_path, {"files": list(merged.values())})
                self.update(force=True)
                # The small official sample becomes usable before the large archives finish.
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
                    pending = [pool.submit(self.fetch, item) for item in sample]
                    try:
                        for future in concurrent.futures.as_completed(pending):
                            future.result()
                            self._count_segments()
                    except BaseException:
                        self.abort.set()
                        for future in pending:
                            future.cancel()
                        raise
                self._count_segments()
                atomic_json(manifest_path, {"files": list(merged.values())})
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
                    futures = {pool.submit(self.fetch, item): item for item in full}
                    try:
                        for future in concurrent.futures.as_completed(futures):
                            self.extract(future.result())
                    except BaseException:
                        self.abort.set()
                        for future in futures:
                            future.cancel()
                        raise
                self.update(force=True, status="completed", current_file="", message="Official comma2k19 download and extraction completed.", bytes_per_second=0)
            except DownloadPaused:
                self.abort.set()
                self.update(force=True, status="paused", message="Download paused. Resume to continue partial files.")
            except Exception as exc:
                self.abort.set()
                self.update(force=True, status="error", message=f"{type(exc).__name__}: {exc}")
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("all", "sample", "full"), default="all")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    Downloader(workers=args.workers).run(args.scope)


if __name__ == "__main__":
    main()
