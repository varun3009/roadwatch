"""Local FastAPI interface; run with python run_roadwatch.py."""
from __future__ import annotations

import asyncio
import copy
import csv
import hashlib
import io
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ConfigStore, number, effective_calibration
from .datasets import DatasetCatalog, parse_reference_file, validate_reference_mapping
from .catalog import UnifiedCatalog
from .comma_download import download_status, start_download, pause_download
from .engine import Engine
from .geometry import calibration_from_dict, project_pixel, locate_hazard, calibrate_checkerboard
from .comma_geometry import project_comma_pixel
from .storage import Store
from .replay import ReplayService, make_replay_router
from .submissions import SubmissionService, make_submission_router
from .replay_detection import ReplayDetectionService, make_replay_detection_router
from .reports import HazardReport, incident_report_html

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "roadwatch_data"
config_store = ConfigStore(RUNTIME / "config.json")
catalog = UnifiedCatalog(ROOT)
store = Store(RUNTIME / "roadwatch.sqlite3")
engine = Engine(ROOT, config_store, catalog, store)
replay_service = ReplayService(ROOT, catalog, store)
submission_service = SubmissionService(ROOT, store, config_store, engine)
replay_detection_service = ReplayDetectionService(ROOT, catalog, store, config_store, engine, submission_service, replay_service)


@asynccontextmanager
async def lifespan(app):
    yield
    engine.stop()
    replay_detection_service.stop()
    submission_service.stop()
    replay_service.close()


app = FastAPI(title="RoadWatch", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(make_replay_router(replay_service))
app.include_router(make_replay_detection_router(replay_detection_service))
app.include_router(make_submission_router(submission_service))


@app.middleware("http")
async def local_origin(request: Request, call_next):
    # The server binds loopback. Prevent unrelated web pages controlling the local camera.
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and urlsplit(origin).netloc != request.headers.get("host"):
        return Response("Cross-origin control is disabled", status_code=403)
    return await call_next(request)


def bad_request(exc):
    return HTTPException(status_code=409 if isinstance(exc, RuntimeError) else 422, detail=str(exc))


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/bootstrap")
def bootstrap():
    return {"config": config_store.value, "recordings": catalog.recordings(), "dataset": catalog.summary(),
            "model": engine.model_info, "state": engine.snapshot(),
            "references": catalog.reference_events("33") + store.references(),
            "datasets": catalog.datasets(), "download": download_status(ROOT)}


_download_catalog_marker = None


def dataset_snapshot(force=False):
    global _download_catalog_marker
    download = download_status(ROOT)
    marker = (download.get("segments_ready"), download.get("status") == "completed")
    if not engine.active() and (force or marker != _download_catalog_marker):
        catalog.refresh()
        _download_catalog_marker = marker
    return {"recordings": catalog.recordings(), "datasets": catalog.datasets(),
            "dataset": catalog.summary(), "download": download}


@app.get("/api/datasets")
def datasets():
    return dataset_snapshot()


@app.post("/api/datasets/refresh")
def refresh_datasets():
    if engine.active():
        raise HTTPException(409, "Stop detection before refreshing the dataset list")
    return dataset_snapshot(force=True)


@app.post("/api/datasets/download/start")
def begin_dataset_download(value: dict):
    try:
        return {"download": start_download(ROOT, scope=value.get("scope", "all"))}
    except (ValueError, OSError) as exc:
        raise bad_request(exc) from exc


@app.post("/api/datasets/download/stop")
def stop_dataset_download():
    return {"download": pause_download(ROOT)}


@app.get("/api/state")
def state():
    return engine.snapshot()


@app.post("/api/config")
def save_config(value: dict):
    with engine.lock:
        if engine.active() or submission_service.active():
            raise HTTPException(409, "Stop detection, close replay, or wait for the current upload before changing settings")
        try:
            saved = config_store.save(value)
            engine.model_info = {"path": saved["detection"]["model_path"], "classes": [], "status": "Load on start"}
            return saved
        except ValueError as exc:
            raise bad_request(exc) from exc


@app.post("/api/session/start")
def start_session(value: dict):
    try:
        with engine.lock:
            if submission_service.active():
                raise RuntimeError("Close detection replay or wait for the current upload to finish")
            return engine.start(value)
    except (ValueError, TypeError, RuntimeError) as exc:
        raise bad_request(exc) from exc


@app.post("/api/session/stop")
def stop_session():
    return engine.stop()


@app.get("/api/recordings/{recording_id}")
def recording(recording_id: str):
    if recording_id not in {str(r["id"]) for r in catalog.recordings()}:
        raise HTTPException(404, "Recording not found")
    record = catalog.recording(recording_id)
    references = catalog.reference_events(recording_id) + [r for r in store.references() if r.get("recording_id") is None or str(r["recording_id"]) == recording_id]
    return {"track": catalog.gps_track(recording_id), "references": references,
            "recording": record, "has_labels": record.get("has_labels", True),
            "calibration": effective_calibration(config_store.value, catalog.calibration_for(recording_id)), "calibration_metadata": catalog.calibration_metadata(recording_id)}


@app.post("/api/pose")
def update_pose(value: dict):
    try:
        pose = {"latitude": number(value.get("latitude"), "Latitude", -90, 90),
                "longitude": number(value.get("longitude"), "Longitude", -180, 180),
                "heading_deg": number(value.get("heading_deg"), "Heading", 0, 360),
                "accuracy_m": number(value.get("accuracy_m", 5), "Accuracy", 0, 10000)}
        if value.get("timestamp_ms") is not None:
            pose["timestamp_ms"] = number(value["timestamp_ms"], "GPS Unix timestamp in milliseconds", 0, 100_000_000_000_000)
        engine.set_pose(pose)
        return {"accepted": True, "pose": pose}
    except ValueError as exc:
        raise bad_request(exc) from exc


@app.post("/api/calibration/probe")
def probe(value: dict):
    try:
        current = engine.snapshot()
        w = int(number(value.get("frame_width", current["frame_width"]), "Frame width", 1, 20000))
        h = int(number(value.get("frame_height", current["frame_height"]), "Frame height", 1, 20000))
        u, v = number(value.get("u"), "Pixel u", 0, w - 1), number(value.get("v"), "Pixel v", 0, h - 1)
        calibration = calibration_from_dict(current.get("active_calibration") or config_store.value["calibration"])
        pose = current.get("pose")
        if not pose and config_store.value["pose"]["mode"] == "manual":
            pose = config_store.value["pose"]
        if current.get("dataset_id") == "comma2k19":
            projection = project_comma_pixel(u, v, w, h, calibration, pose or {})
            location = projection
        else:
            projection = project_pixel(u, v, w, h, calibration)
            location = locate_hazard(projection, pose, calibration) if pose else {"valid": False, "reason": "No valid GPS pose"}
        return {**projection, "projection": projection, "location": location, "latitude": location.get("latitude"), "longitude": location.get("longitude")}
    except (ValueError, TypeError) as exc:
        raise bad_request(exc) from exc


async def bounded_read(upload, limit=20 * 1024 * 1024):
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, "Upload must be smaller than 20 MB")
    return data


@app.post("/api/references/import")
async def import_references(file: UploadFile = File(...), mapping: str = Form("{}")):
    if engine.active():
        raise HTTPException(409, "Stop the session before importing reference records")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".csv", ".xlsx"):
        raise HTTPException(422, "Choose an XLSX workbook or CSV file")
    data = await bounded_read(file)
    try:
        fields = validate_reference_mapping(json.loads(mapping))
        source_name = Path(file.filename or f"references{suffix}").name
        import_id = hashlib.sha256(data + b"\0" + json.dumps(fields, sort_keys=True, allow_nan=False).encode()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="roadwatch-") as temp:
            # Only the basename is used; uploads cannot overwrite project sources.
            path = Path(temp) / source_name
            path.write_bytes(data)
            result = await asyncio.to_thread(parse_reference_file, path, fields or None)
        for record in result.get("records", []):
            sheet = record.get("source_sheet")
            record["source"] = f"{source_name}#{sheet}" if suffix == ".xlsx" and sheet else source_name
        if result.get("records"):
            with engine.lock:
                # Parsing runs in a worker; another request may have started a
                # session while the file was being read. Keep its refs stable.
                if engine.active():
                    raise HTTPException(409, "Stop the session before importing reference records")
                result["records"] = store.save_references(source_name, result["records"], import_id=import_id)
        result["import_id"] = import_id
        result["imported_count"] = len(result.get("records", []))
        return result
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise bad_request(exc) from exc


@app.post("/api/calibration/checkerboard")
async def checkerboard(images: list[UploadFile] = File(...), columns: int = Form(9), rows: int = Form(6), square_size_m: float = Form(0.025)):
    if engine.active():
        raise HTTPException(409, "Stop the session before calibrating")
    if len(images) > 40:
        raise HTTPException(422, "Upload at most 40 checkerboard images")
    if not 3 <= columns <= 30 or not 3 <= rows <= 30:
        raise HTTPException(422, "Inner corner counts must be 3–30")
    try:
        number(square_size_m, "Square size in meters", 0.001, 1)
        decoded = []
        for upload in images:
            image = cv2.imdecode(np.frombuffer(await bounded_read(upload, 10 * 1024 * 1024), dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.size > 36_000_000:
                raise ValueError("Use valid checkerboard images up to 12 megapixels")
            decoded.append(image)
        result = await asyncio.to_thread(calibrate_checkerboard, decoded, columns, rows, square_size_m)
        intrinsic_keys = ("width", "height", "fx", "fy", "cx", "cy", "distortion")
        return {**result, "calibration": {key: result[key] for key in intrinsic_keys}}
    except ValueError as exc:
        raise bad_request(exc) from exc


@app.get("/api/hazards")
def saved_hazards(session_id: str | None = None):
    return {"hazards": store.hazards(session_id)}


@app.get("/api/hazard-report")
def hazard_report(hazard_id: str):
    hazard = store.hazard(hazard_id)
    if hazard is None:
        raise HTTPException(404, "Hazard not found")
    return hazard


@app.put("/api/hazard-report")
def save_hazard_report(hazard_id: str, report: HazardReport):
    hazard = store.save_hazard_report(hazard_id, report.model_dump())
    if hazard is None:
        raise HTTPException(404, "Hazard not found")
    return hazard


@app.get("/api/incident-report", response_class=HTMLResponse)
def incident_report(hazard_id: str):
    hazard = hazard_report(hazard_id)
    return HTMLResponse(incident_report_html(hazard, store.evidence(hazard_id)), headers={"Cache-Control": "no-store"})


@app.get("/api/evidence/{evidence_id}.jpg")
def evidence_image(evidence_id: int):
    image = store.evidence_image(evidence_id)
    if image is None:
        raise HTTPException(404, "Evidence image not found")
    jpeg, metadata = image
    return Response(jpeg, media_type="image/jpeg", headers={
        "Cache-Control": "private, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
        "ETag": f'"{metadata["sha256"]}"',
    })


@app.get("/api/export.csv")
def export_csv(session_id: str | None = None):
    output = io.StringIO(newline="")
    fields = ["id", "session_id", "recording_id", "dataset_id", "has_reference_labels", "class_name", "class_ambiguous", "track_id", "latitude", "longitude", "distance_m",
              "confidence", "observations", "located_observations", "first_seen_s", "last_seen_s", "provisional",
              "gps_accuracy_m", "match_reference_id", "match_distance_m", "accuracy_note"]
    writer = csv.DictWriter(output, fields)
    writer.writeheader()
    for hazard in store.hazards(session_id):
        row = {key: hazard.get(key) for key in fields}
        match = hazard.get("match") or {}
        row["match_reference_id"] = match.get("reference_id", match.get("id"))
        row["match_distance_m"] = match.get("distance_m")
        for key, value in row.items():
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                row[key] = "'" + value
        writer.writerow(row)
    return Response("\ufeff" + output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=roadwatch-hazards.csv"})


@app.get("/video.mjpg")
async def video():
    async def frames():
        previous = None
        while True:
            with engine.lock:
                jpeg = engine.jpeg
            if jpeg and jpeg is not previous:
                previous = jpeg
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
            await asyncio.sleep(0.05)
    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store"})


@app.websocket("/ws")
async def websocket(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    if origin and urlsplit(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(engine.snapshot())
            await asyncio.sleep(0.2)
    except (WebSocketDisconnect, RuntimeError):
        pass
