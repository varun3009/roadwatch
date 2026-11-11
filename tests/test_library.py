import csv
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from roadwatch.library_api import make_library_router
from roadwatch.storage import Store


@pytest.fixture
def saved(tmp_path):
    storage = Store(tmp_path / "roadwatch.sqlite3")
    storage.start_session("session", "dataset:33", {"private": "not-for-complaint"})
    storage.save_hazard("session", {"id": "session:1", "session_id": "session", "class_name": "Pothole",
                                   "latitude": 37.7, "longitude": -122.4, "confidence": 0.8, "provisional": True})
    application = FastAPI()
    application.include_router(make_library_router(storage, tmp_path))
    return storage, TestClient(application)


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
    storage, _ = saved
    with ThreadPoolExecutor(max_workers=8) as pool:
        evidence = list(pool.map(lambda number: storage.save_evidence("session:1", b"jpeg-" + bytes([number]),
                                                   {"frame_index": number, "time_s": number / 20}), range(40)))
    assert sum(item is not None for item in evidence) == 10
    assert len(storage.evidence("session:1")) == 10
    assert storage.hazard("session:1")["evidence_count"] == 10
    assert storage.save_evidence("session:1", b"later frame", {"frame_index": 100}) is None
    with storage.connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="ten frames"):
            db.execute("INSERT INTO hazard_evidence(hazard_id,frame_key,jpeg,metadata,sha256) VALUES(?,?,?,?,?)",
                       ("session:1", "bypass", b"data", "{}", "abc"))


def test_duplicate_frame_and_reopen_preserve_original_evidence(saved):
    storage, client = saved
    first = storage.save_evidence("session:1", b"first jpeg", {"frame_index": 3, "bbox": [1, 2, 3, 4]})
    duplicate = storage.save_evidence("session:1", b"duplicate jpeg", {"frame_index": 3})
    assert first["id"] == duplicate["id"]
    reopened = Store(storage.path)
    image, metadata = reopened.evidence_image(first["id"])
    assert image == b"first jpeg"
    assert metadata["bbox"] == [1, 2, 3, 4]
    response = client.get(first["url"])
    assert response.content == image
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert reopened.hazard("session:1")["thumbnail_url"] == first["url"]


@pytest.mark.parametrize("metadata", [{"frame_index": -1}, {"frame_index": True}, {"frame_index": 1.5},
                                      {"time_s": -1}, {"time_s": float("nan")}])
def test_invalid_evidence_metadata_is_rejected(saved, metadata):
    storage, _ = saved
    with pytest.raises(ValueError):
        storage.save_evidence("session:1", b"jpeg", metadata)


def test_review_metadata_survives_continuing_engine_updates(saved):
    storage, client = saved
    response = client.patch("/api/library/session:1", json={"status": "reported", "severity": "high", "address": "Main Street", "notes": "Needs inspection"})
    assert response.status_code == 200
    storage.save_hazard("session", {"id": "session:1", "session_id": "session", "class_name": "Pothole", "confidence": 0.9})
    hazard = storage.hazard("session:1")
    assert hazard["status"] == "reported"
    assert hazard["confidence"] == 0.9
    assert hazard["notes"] == "Needs inspection"
    assert client.patch("/api/library/session:1", json={"latitude": 0}).status_code == 422
    assert client.patch("/api/library/session:1", json={"status": "submitted"}).status_code == 422
    assert client.patch("/api/library/session:1", json={"notes": 1}).status_code == 422
    assert client.patch("/api/library/missing", json={"notes": "text"}).status_code == 404


def test_nearby_search_sorts_by_distance_and_excludes_unlocated(saved):
    storage, client = saved
    for identity, lat, lon in (("near", 37.7001, -122.4), ("far", 37.705, -122.4), ("unknown", None, None)):
        storage.save_hazard("session", {"id": identity, "class_name": "Crack", "latitude": lat, "longitude": lon})
    result = client.get("/api/library", params={"latitude": 37.7, "longitude": -122.4, "radius_m": 100}).json()
    assert [hazard["id"] for hazard in result["hazards"]] == ["session:1", "near"]
    assert result["hazards"][1]["nearby_distance_m"] == pytest.approx(11.12, abs=0.1)
    assert result["counts"]["total"] == 4
    assert result["counts"]["located"] == 3
    assert result["total"] == 2
    assert client.get("/api/library", params={"latitude": 37.7}).status_code == 422
    assert client.get("/api/library", params={"latitude": "nan", "longitude": 0}).status_code == 422
    assert client.get("/api/library", params={"radius_m": -1}).status_code == 422


def test_filters_and_pagination_return_total_before_page(saved):
    storage, client = saved
    storage.save_hazard("session", {"id": "second", "class_name": "Pothole", "notes": "Park Street"})
    storage.update_hazard_review("second", {"notes": "Park Street", "status": "resolved"})
    assert client.get("/api/library", params={"search": "PARK STREET"}).json()["total"] == 1
    assert client.get("/api/library", params={"class_name": "pothole", "status": "resolved"}).json()["hazards"][0]["id"] == "second"
    page = client.get("/api/library", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 2 and len(page["hazards"]) == 1
    assert page["counts"]["resolved"] == 1
    assert client.get("/api/library", params={"limit": 500}).status_code == 422


def test_session_metadata_is_persisted_and_config_excluded_from_public_api(saved):
    storage, client = saved
    storage.session_update("session", media_kind="video", source_path="E:/private/video.mp4", replay_available=True)
    session = storage.session("session")
    assert session["metadata"]["media_kind"] == session["media_kind"] == "video"
    assert session["config"]["private"] == "not-for-complaint"
    public = client.get("/api/sessions").json()["sessions"][0]
    assert "config" not in public and "source_path" not in public
    assert public["hazard_count"] == 1 and public["replay_available"]
    with pytest.raises(ValueError):
        storage.session_update("session", id="overwrite")


def test_complaint_draft_is_escaped_self_contained_and_does_not_submit(saved):
    storage, client = saved
    storage.update_hazard_review("session:1", {"notes": '<script>alert("x")</script>', "title": "Road repair <request>",
                                              "reporter_name": "Test reporter", "municipal_authority": "Example authority"})
    storage.save_evidence("session:1", b"jpeg bytes", {"frame_index": 30, "time_s": 1.5})
    response = client.get("/api/library/session:1/complaint.html")
    assert response.status_code == 200
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "data:image/jpeg;base64," in response.text
    assert "not submitted" in response.text
    assert "accuracy is unknown" in response.text
    assert "openstreetmap.org/?mlat=37.70000000" in response.text
    assert "not-for-complaint" not in response.text
    assert storage.hazard("session:1")["status"] == "open"


def test_zip_contains_relative_photos_and_machine_readable_evidence(saved):
    storage, client = saved
    storage.update_hazard_review("session:1", {"title": "=HYPERLINK(1)", "address": "Real landmark"})
    original = b"original JPEG bytes"
    storage.save_evidence("session:1", original, {"frame_index": 10, "time_s": 0.5})
    response = client.get("/api/library/session:1/complaint.zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"complaint.html", "hazard.json", "hazard.csv", "README.txt", "evidence/frame-01.jpg"}
        assert archive.read("evidence/frame-01.jpg") == original
        report = json.loads(archive.read("hazard.json"))
        assert report["submitted"] is False
        assert "config" not in report and "not-for-complaint" not in archive.read("hazard.json").decode()
        assert report["evidence"][0]["sha256"] == storage.evidence("session:1")[0]["sha256"]
        assert 'src="evidence/frame-01.jpg"' in archive.read("complaint.html").decode()
        rows = list(csv.DictReader(io.StringIO(archive.read("hazard.csv").decode("utf-8-sig"))))
        assert rows[0]["title"] == "'=HYPERLINK(1)"
    assert client.get("/api/library/missing/complaint.zip").status_code == 404
    assert client.get("/api/evidence/999.jpg").status_code == 404


def test_upload_complaint_preserves_human_readable_capture_proxy_provenance(saved):
    storage, client = saved
    note = "Reporter-supplied capture location. A fixed coordinate applies to the whole upload and may differ from the hazard position; no road-distance projection was applied."
    storage.save_hazard("session", {"id": "upload:1", "session_id": "session", "class_name": "Pothole",
                                   "source_type": "upload_video", "latitude": 37.7, "longitude": -122.4,
                                   "location_reference": "capture", "location_method": "fixed_capture_location_proxy",
                                   "location_note": note, "provisional": True})
    response = client.get("/api/library/upload:1/complaint.html")
    assert response.status_code == 200
    assert note in response.text
    assert "Coordinate provenance not supplied" not in response.text
    assert "Projected from synchronized camera/GPS pose" not in response.text
    response = client.get("/api/library/upload:1/complaint.zip")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        hazard = json.loads(archive.read("hazard.json"))["hazard"]
        assert hazard["location_reference"] == "capture"
        assert hazard["location_method"] == "fixed_capture_location_proxy"
        assert hazard["location_note"] == note


def test_five_hundred_character_address_and_contact_survive_review_and_restart(saved):
    storage, client = saved
    address = "A" * 500
    contact = "C" * 500
    response = client.patch("/api/library/session:1", json={"address": address, "reporter_contact": contact, "notes": "N" * 5000})
    assert response.status_code == 200
    reopened = Store(storage.path)
    assert reopened.hazard("session:1")["address"] == address
    assert reopened.hazard("session:1")["reporter_contact"] == contact
    assert len(reopened.hazard("session:1")["notes"]) == 5000
    assert client.patch("/api/library/session:1", json={"address": address + "A"}).status_code == 422
