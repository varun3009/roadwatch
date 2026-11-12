from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from roadwatch.storage import Store


@pytest.fixture
def saved(tmp_path):
    storage = Store(tmp_path / "roadwatch.sqlite3")
    storage.start_session("session", "dataset:33", {"camera": "dashcam"})
    storage.save_hazard("session", {"id": "session:1", "session_id": "session", "class_name": "Pothole",
                                   "latitude": 37.7, "longitude": -122.4, "confidence": 0.8, "provisional": True})
    return storage


def test_existing_database_is_migrated_without_losing_hazards(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE sessions(id TEXT PRIMARY KEY,created_at TEXT,source TEXT,config TEXT,status TEXT);"
                         "CREATE TABLE hazards(id TEXT PRIMARY KEY,session_id TEXT,payload TEXT NOT NULL);")
        db.execute("INSERT INTO sessions VALUES('old','2024-01-01 10:00:00','dataset:1','{}','completed')")
        db.execute("INSERT INTO hazards VALUES('old:1','old',?)", (json.dumps({"id": "old:1", "class_name": "Pothole"}),))
    storage = Store(path)
    hazard = storage.hazard("old:1")
    assert hazard["class_name"] == "Pothole"
    assert hazard["created_at"] == "2024-01-01 10:00:00"
    assert hazard["evidence_count"] == 0
    assert hazard["thumbnail_url"] is None
    assert hazard["status"] == "open"


def test_concurrent_evidence_saves_never_exceed_ten(saved):
    with ThreadPoolExecutor(max_workers=8) as pool:
        evidence = list(pool.map(lambda number: saved.save_evidence("session:1", b"jpeg-" + bytes([number]),
                                 {"frame_index": number, "time_s": number / 20}), range(40)))
    assert sum(item is not None for item in evidence) == 10
    assert len(saved.evidence("session:1")) == 10
    assert saved.hazard("session:1")["evidence_count"] == 10
    assert saved.save_evidence("session:1", b"later frame", {"frame_index": 100}) is None
    with saved.connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="ten frames"):
            db.execute("INSERT INTO hazard_evidence(hazard_id,frame_key,jpeg,metadata,sha256) VALUES(?,?,?,?,?)",
                       ("session:1", "bypass", b"data", "{}", "abc"))


def test_duplicate_frame_and_reopen_preserve_original_evidence(saved):
    first = saved.save_evidence("session:1", b"first jpeg", {"frame_index": 3, "bbox": [1, 2, 3, 4]})
    duplicate = saved.save_evidence("session:1", b"duplicate jpeg", {"frame_index": 3})
    assert first["id"] == duplicate["id"]
    reopened = Store(saved.path)
    image, metadata = reopened.evidence_image(first["id"])
    assert image == b"first jpeg"
    assert metadata["bbox"] == [1, 2, 3, 4]
    assert reopened.hazard("session:1")["thumbnail_url"] == first["url"]


@pytest.mark.parametrize("metadata", [{"frame_index": -1}, {"frame_index": True}, {"frame_index": 1.5},
                                      {"time_s": -1}, {"time_s": float("nan")}])
def test_invalid_evidence_metadata_is_rejected(saved, metadata):
    with pytest.raises(ValueError):
        saved.save_evidence("session:1", b"jpeg", metadata)


def test_legacy_review_metadata_survives_engine_updates(saved):
    with saved.connect() as db:
        db.execute("INSERT INTO hazard_review(hazard_id,payload) VALUES(?,?)",
                   ("session:1", json.dumps({"status": "reported", "severity": "high", "notes": "Needs inspection"})))
    reopened = Store(saved.path)
    reopened.save_hazard("session", {"id": "session:1", "class_name": "Pothole", "confidence": 0.9})
    hazard = reopened.hazard("session:1")
    assert hazard["status"] == "reported"
    assert hazard["confidence"] == 0.9
    assert hazard["notes"] == "Needs inspection"


def test_session_metadata_is_persisted(saved):
    saved.session_update("session", media_kind="video", source_path="E:/private/video.mp4")
    session = saved.session("session")
    assert session["metadata"]["media_kind"] == session["media_kind"] == "video"
    assert session["config"]["camera"] == "dashcam"
    assert saved.sessions()[0]["hazard_count"] == 1
    with pytest.raises(ValueError):
        saved.session_update("session", id="overwrite")
