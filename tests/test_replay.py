"""Replay really decodes forward/backward frames without running inference."""
from pathlib import Path
from types import SimpleNamespace

import cv2
from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np
import pytest

from roadwatch.replay import ReplayService, make_replay_router


@pytest.fixture
def replay(tmp_path):
    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (80, 60))
    assert writer.isOpened()
    for index in range(10):
        writer.write(np.full((60, 80, 3), index * 20, dtype=np.uint8))
    writer.release()
    record = {"id": "1", "video_path": str(path), "label": "Test road"}
    catalog = SimpleNamespace(recording=lambda identity: record if identity == "1" else None,
                              recordings=lambda: [record])
    sessions = [{"id": "saved", "source": "file:" + str(path), "metadata": {}}]
    storage = SimpleNamespace(session=lambda identity: next((s for s in sessions if s["id"] == identity), None),
                              sessions=lambda: sessions)
    service = ReplayService(tmp_path, catalog, storage)
    app = FastAPI()
    app.include_router(make_replay_router(service))
    with TestClient(app) as client:
        yield SimpleNamespace(service=service, client=client, record=record, catalog=catalog,
                              sessions=sessions, path=path, root=tmp_path)
    service.close()


def brightness(response):
    assert response.status_code == 200, response.text
    return cv2.imdecode(np.frombuffer(response.content, dtype=np.uint8), cv2.IMREAD_COLOR).mean()


def test_real_decoder_can_replay_and_scrub_backwards(replay):
    sources = replay.client.get("/api/replay/sources").json()["sources"]
    assert {s["id"] for s in sources} == {"dataset:1", "session:saved"}
    info = replay.client.get("/api/replay/info", params={"source_id": "dataset:1"}).json()
    assert info["duration_s"] == 1
    assert info["fps"] == 10
    assert info["audio"] is False
    assert not any(key.startswith("_") for key in info)
    frames = [replay.client.get("/api/replay/frame", params={"source_id": "session:saved", "time_s": t}) for t in (0, .8, .2, .2)]
    assert brightness(frames[0]) < 3
    assert 155 < brightness(frames[1]) < 165
    assert 35 < brightness(frames[2]) < 45
    assert frames[2].content == frames[3].content
    assert frames[2].headers["x-frame-time"] == "0.2"


def test_native_clock_selects_frame_independent_of_reported_video_fps(replay):
    replay.record.update(id="comma2k19-test", frame_count=10, fps=20, duration_s=1.8)
    replay.catalog.recording = lambda identity: replay.record if identity == "comma2k19-test" else None
    replay.catalog.frame_time_s = lambda identity, index: index * .2
    result = replay.client.get("/api/replay/frame", params={"source_id": "dataset:comma2k19-test", "time_s": .81})
    assert 75 < brightness(result) < 85
    assert result.headers["x-frame-time"] == "0.8"
    info = replay.client.get("/api/replay/info", params={"source_id": "dataset:comma2k19-test"}).json()
    assert info["duration_s"] == 2
    last = replay.client.get("/api/replay/frame", params={"source_id": "dataset:comma2k19-test", "time_s": 1.95})
    assert 175 < brightness(last) < 185


@pytest.mark.parametrize("value", [-1, "nan", "inf", 2])
def test_out_of_timeline_requests_are_rejected(replay, value):
    response = replay.client.get("/api/replay/frame", params={"source_id": "dataset:1", "time_s": value})
    assert response.status_code == 422


def test_streams_photos_and_paths_outside_project_are_not_replay_sources(replay, tmp_path):
    replay.sessions.extend([
        {"id": "stream", "source": "stream:camera", "metadata": {}},
        {"id": "photo", "source": "upload", "metadata": {"media_kind": "photo", "source_path": str(replay.path)}},
        {"id": "escape", "source": "upload", "metadata": {"source_path": str(tmp_path.parent / "outside.mp4")}},
    ])
    sources = replay.client.get("/api/replay/sources").json()["sources"]
    assert {source["id"] for source in sources} == {"dataset:1", "session:saved"}
    assert replay.client.get("/api/replay/video", params={"source_id": "session:stream"}).status_code == 404
    assert replay.client.get("/api/replay/video", params={"source_id": str(replay.path)}).status_code == 404


def test_original_video_download_matches_preserved_source(replay):
    response = replay.client.get("/api/replay/video", params={"source_id": "dataset:1"})
    assert response.status_code == 200
    assert response.content == replay.path.read_bytes()
    ranged = replay.client.get("/api/replay/video", params={"source_id": "dataset:1"}, headers={"range": "bytes=0-31"})
    assert ranged.status_code == 206
    assert ranged.content == replay.path.read_bytes()[:32]
