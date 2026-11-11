"""Downloader checks use temporary roots and fake HTTP; no real transfer/control."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import zipfile

import httpx
import pytest

from roadwatch.comma_download import DownloadPaused, Downloader, atomic_json, safe_target


def test_extraction_repairs_same_size_corrupt_cached_member(tmp_path):
    downloader = Downloader(tmp_path)
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr("Chunk_1/route|time/0/frame_times", b"good")
    target = safe_target(downloader.destination, "Chunk_1/route|time/0/frame_times")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"bad!")
    downloader.extract(archive)
    assert target.read_bytes() == b"good"


def test_extracted_marker_does_not_hide_deleted_member(tmp_path):
    downloader = Downloader(tmp_path)
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr("Chunk_1/route/0/frame_times", b"data")
    downloader.extract(archive)
    target = downloader.destination / "Chunk_1/route/0/frame_times"
    target.unlink()
    downloader.extract(archive)
    assert target.read_bytes() == b"data"


def manifest(data, local="_archives/fixture.bin", github=False):
    digest = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() if github else hashlib.sha256(data).hexdigest()
    return {"url": "https://example.invalid/publisher/fixture", "local": local,
            "size": len(data), "hash": digest, "github": github}


class FakeClient:
    def __init__(self, responder, calls):
        self.responder, self.calls = responder, calls

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    @contextmanager
    def stream(self, method, url, headers=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}})
        response = self.responder(self.calls[-1], len(self.calls))
        try:
            yield response
        finally:
            response.close()


def response(status=200, content=b"", headers=None, stream=None):
    options = {"content": content} if stream is None else {"stream": stream}
    return httpx.Response(status, headers=headers, request=httpx.Request("GET", "https://example.invalid/file"), **options)


def fake_http(monkeypatch, responder):
    calls = []
    monkeypatch.setattr("roadwatch.comma_download.httpx.Client", lambda **unused: FakeClient(responder, calls))
    return calls


def test_safe_target_normalizes_official_unix_route_for_windows(tmp_path):
    target = safe_target(tmp_path, "Chunk_1/b0c9d2329ad1606b|2018-08-02--08-34-47/40/video.hevc")
    assert target == tmp_path / "Chunk_1" / "b0c9d2329ad1606b_2018-08-02--08-34-47" / "40" / "video.hevc"
    assert safe_target(tmp_path, "NUL.txt") == tmp_path / "_NUL.txt"
    assert safe_target(tmp_path, "folder/x:stream/a?b") == tmp_path / "folder" / "x_stream" / "a_b"


@pytest.mark.parametrize("relative", ["../escape", "a/../../escape", "/absolute", "C:/escape", "C:escape",
                                      "\\\\server\\share\\escape", "", "...", "  ", "a/..\\..\\escape"])
def test_safe_target_rejects_traversal_absolute_and_empty_names(tmp_path, relative):
    with pytest.raises(ValueError):
        safe_target(tmp_path, relative)


def test_publisher_hashes_use_git_blob_header_and_sha256(tmp_path):
    downloader = Downloader(tmp_path)
    data = b"official fixture\n"
    path = tmp_path / "hash-fixture"
    path.write_bytes(data)
    assert downloader._hash(path) == hashlib.sha256(data).hexdigest()
    assert downloader._hash(path, github=True) == hashlib.sha1(b"blob 17\0" + data).hexdigest()


def test_download_promotes_only_complete_publisher_verified_data(tmp_path, monkeypatch):
    downloader = Downloader(tmp_path)
    data = b"complete publisher fixture"
    calls = fake_http(monkeypatch, lambda call, number: response(content=data))
    item = manifest(data)
    target = downloader.fetch(item)
    assert target.read_bytes() == data
    assert not Path(str(target) + ".partial").exists()
    assert downloader.state["files_completed"] == 1
    assert calls[0]["headers"] == {}
    assert "download=true" in calls[0]["url"]


def test_resume_requests_exact_partial_offset(tmp_path, monkeypatch):
    downloader = Downloader(tmp_path)
    data = b"partial-and-remainder"
    item = manifest(data)
    target = safe_target(downloader.destination, item["local"])
    target.parent.mkdir(parents=True)
    partial = Path(str(target) + ".partial")
    partial.write_bytes(data[:7])
    calls = fake_http(monkeypatch, lambda call, number: response(
        status=206, content=data[7:], headers={"Content-Range": f"bytes 7-{len(data)-1}/{len(data)}"},
    ))
    assert downloader.fetch(item).read_bytes() == data
    assert calls[0]["headers"] == {"Range": "bytes=7-"}


@pytest.mark.parametrize("status,headers", [(200, {}), (206, {"Content-Range": "bytes 2-15/16"})])
def test_wrong_resume_range_keeps_partial_and_does_not_promote(tmp_path, monkeypatch, status, headers):
    downloader = Downloader(tmp_path)
    data = b"known-total-data"
    item = manifest(data)
    target = safe_target(downloader.destination, item["local"])
    target.parent.mkdir(parents=True)
    partial = Path(str(target) + ".partial")
    partial.write_bytes(data[:5])
    fake_http(monkeypatch, lambda call, number: response(status, content=data, headers=headers))
    with pytest.raises(ValueError, match="range"):
        downloader.fetch(item)
    assert not target.exists()
    assert partial.read_bytes() == data[:5]


def test_wrong_publisher_hash_is_never_promoted(tmp_path, monkeypatch):
    downloader = Downloader(tmp_path)
    expected, received = b"correct-data", b"corrupt-data"
    item = manifest(expected)
    fake_http(monkeypatch, lambda call, number: response(content=received))
    with pytest.raises(ValueError, match="checksum"):
        downloader.fetch(item)
    target = safe_target(downloader.destination, item["local"])
    assert not target.exists()
    assert Path(str(target) + ".partial").read_bytes() == received


def test_existing_good_file_is_verified_without_network(tmp_path, monkeypatch):
    downloader = Downloader(tmp_path)
    data = b"existing valid data"
    item = manifest(data, github=True)
    target = safe_target(downloader.destination, item["local"])
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    monkeypatch.setattr("roadwatch.comma_download.httpx.Client", lambda **unused: pytest.fail("Existing verified file must not redownload"))
    assert downloader.fetch(item) == target


def test_interrupted_transfer_resumes_saved_bytes(tmp_path, monkeypatch):
    class InterruptedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"a" * (1024 * 1024)
            raise httpx.ReadError("simulated connection loss")

    downloader = Downloader(tmp_path)
    downloader.abort.wait = lambda seconds: False
    data = b"a" * (1024 * 1024) + b"tail"

    def responder(call, number):
        if number == 1:
            return response(stream=InterruptedStream())
        return response(206, content=b"tail", headers={"Content-Range": f"bytes {1024*1024}-{len(data)-1}/{len(data)}"})

    calls = fake_http(monkeypatch, responder)
    assert downloader.fetch(manifest(data)).read_bytes() == data
    assert len(calls) == 2
    assert calls[1]["headers"] == {"Range": f"bytes={1024*1024}-"}


def test_pause_between_blocks_retains_downloaded_prefix(tmp_path, monkeypatch):
    downloader = Downloader(tmp_path)
    prefix = b"a" * (1024 * 1024)

    class PausingStream(httpx.SyncByteStream):
        def __iter__(self):
            yield prefix
            # The isolated temp root has its own control file; no running worker
            # or application dataset is touched by this regression test.
            downloader.pause_path.write_text("test pause", encoding="utf-8")
            yield b"tail"

    fake_http(monkeypatch, lambda call, number: response(stream=PausingStream()))
    item = manifest(prefix + b"tail")
    with pytest.raises(DownloadPaused):
        downloader.fetch(item)
    target = safe_target(downloader.destination, item["local"])
    assert not target.exists()
    assert Path(str(target) + ".partial").read_bytes() == prefix


@pytest.fixture
def extraction(tmp_path, monkeypatch):
    monkeypatch.setattr("roadwatch.comma_download.shutil.disk_usage", lambda path: SimpleNamespace(free=1024**4))
    return Downloader(tmp_path), tmp_path / "fixture.zip"


def test_zip_routes_are_extracted_with_windows_safe_names(extraction):
    downloader, archive = extraction
    name = "Chunk_1/route|2026-01-01/0/video.hevc"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, b"video fixture")
        for pose_name in ("frame_times", "frame_positions", "frame_orientations"):
            output.writestr("Chunk_1/route|2026-01-01/0/global_pose/" + pose_name, b"pose fixture")
    downloader.extract(archive)
    assert safe_target(downloader.destination, name).read_bytes() == b"video fixture"
    assert Path(str(archive) + ".extracted.json").exists()
    assert downloader.state["segments_ready"] == 1


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/outside", "safe/../../outside"])
def test_unsafe_zip_members_never_escape_destination(extraction, name):
    downloader, archive = extraction
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, b"unsafe")
    with pytest.raises(ValueError):
        downloader.extract(archive)
    assert not (downloader.root / "outside").exists()
    assert not Path(str(archive) + ".extracted.json").exists()


def test_zip_symlink_is_rejected(extraction):
    downloader, archive = extraction
    entry = zipfile.ZipInfo("link")
    entry.create_system = 3
    entry.external_attr = 0o120777 << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(entry, "../../elsewhere")
    with pytest.raises(ValueError, match="symbolic link"):
        downloader.extract(archive)
    assert not (downloader.destination / "link").exists()


def test_zip_normalized_filename_collisions_are_rejected(extraction):
    downloader, archive = extraction
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("route|time/video.hevc", b"one")
        output.writestr("route_time/video.hevc", b"two")
    with pytest.raises(ValueError, match="same Windows path"):
        downloader.extract(archive)
    assert not Path(str(archive) + ".extracted.json").exists()


def test_malformed_zip_is_rejected(extraction):
    downloader, archive = extraction
    archive.write_bytes(b"not an archive")
    with pytest.raises(zipfile.BadZipFile):
        downloader.extract(archive)


def test_crc_error_never_promotes_extracted_file(extraction):
    downloader, archive = extraction
    payload = b"unique crc test payload"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("route/0/video.hevc", payload)
    encoded = archive.read_bytes()
    assert encoded.count(payload) == 1
    archive.write_bytes(encoded.replace(payload, b"X" + payload[1:]))
    with pytest.raises(zipfile.BadZipFile, match="CRC"):
        downloader.extract(archive)
    assert not (downloader.destination / "route" / "0" / "video.hevc").exists()
    assert not Path(str(archive) + ".extracted.json").exists()


def test_worker_error_signals_peers_before_waiting_for_pool_shutdown(tmp_path, monkeypatch):
    downloader = Downloader(tmp_path, workers=2)
    peer_started = threading.Event()
    peer_aborted = []
    first, second = manifest(b"one", "first.zip"), manifest(b"two", "second.zip")
    monkeypatch.setattr(downloader, "full_manifest", lambda: [first, second])
    monkeypatch.setattr(downloader, "extract", lambda archive: None)

    def fetch(item):
        if item is first:
            assert peer_started.wait(2), "Second worker did not start"
            raise ValueError("simulated publisher failure")
        peer_started.set()
        peer_aborted.append(downloader.abort.wait(2))
        return tmp_path / "unused.zip"

    monkeypatch.setattr(downloader, "fetch", fetch)
    with pytest.raises(ValueError, match="simulated publisher failure"):
        downloader.run(scope="full")
    assert peer_aborted == [True]
    assert downloader.state["status"] == "error"


def test_concurrent_atomic_status_writers_leave_valid_json_and_no_temporary_files(tmp_path):
    target = tmp_path / "status.json"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda number: atomic_json(target, {"number": number, "payload": "x" * 2048}), range(24)))
    assert json.loads(target.read_text("utf-8"))["number"] in range(24)
    assert list(tmp_path.glob("*.tmp")) == []


def test_ready_count_accepts_alternate_pose_layout_and_deduplicates_routes(tmp_path):
    downloader = Downloader(tmp_path)
    for chunk, directory, suffix in (("Example_1", "global_pose", ""), ("Chunk_2", "global_pos", ".npy")):
        segment = downloader.destination / chunk / "route_time" / "0"
        (segment / directory).mkdir(parents=True)
        (segment / "video.mp4").write_bytes(b"published video")
        for name in ("frame_times", "frame_positions", "frame_orientations"):
            (segment / directory / (name + suffix)).write_bytes(b"published telemetry")
    incomplete = downloader.destination / "Chunk_2" / "other_route" / "1"
    incomplete.mkdir(parents=True)
    (incomplete / "video.hevc").write_bytes(b"video without pose")
    downloader._count_segments()
    assert downloader.state["segments_ready"] == 1


def test_extraction_publishes_ready_segments_before_archive_finishes(extraction, monkeypatch):
    downloader, archive = extraction
    clock = iter(range(0, 100_000, 10))
    monkeypatch.setattr("roadwatch.comma_download.time.monotonic", lambda: next(clock))
    updates = []
    original_update = downloader.update

    def update(*args, **kwargs):
        if "segments_ready" in kwargs:
            updates.append((kwargs["segments_ready"], Path(str(archive) + ".extracted.json").exists()))
        original_update(*args, **kwargs)

    monkeypatch.setattr(downloader, "update", update)
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("route/0/video.hevc", b"video")
        for name in ("frame_times", "frame_positions", "frame_orientations"):
            output.writestr("route/0/global_pose/" + name, b"pose")
        output.writestr("route/0/processed_log/extra", b"other telemetry")
    downloader.extract(archive)
    assert (1, False) in updates
    assert updates[-1] == (1, True)
