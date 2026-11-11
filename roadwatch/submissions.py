"""Local citizen submissions with bounded media, explicit GPS provenance and jobs.

Uploaded GPS is deliberately never treated as a calibrated road projection.
It is either a user-selected hazard position or the camera location at capture.
"""
from __future__ import annotations

import asyncio
import bisect
import copy
import json
import math
from pathlib import Path
import threading
import time
import uuid

import cv2
import numpy as np
from PIL import Image
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .config import number


MAX_UPLOAD_BYTES = 250 * 1024 * 1024
MAX_PHOTO_BYTES = 25 * 1024 * 1024
MAX_PHOTO_PIXELS = 24_000_000
MAX_VIDEO_SECONDS = 300
MAX_VIDEO_INFERENCES = 900
MAX_GPS_GAP_SECONDS = 3
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}


def _text(value, label, maximum=4000):
    value = str(value or "").strip()
    if len(value) > maximum:
        raise ValueError(f"{label} must have at most {maximum} characters")
    return value


def validate_submission(values):
    """Validate human-entered coordinates before any job or upload is committed."""
    result = dict(values)
    if result.get("mode") not in {"detect", "manual"}:
        raise ValueError("Choose detect or manual submission")
    if result.get("location_reference", "capture") not in {"capture", "hazard"}:
        raise ValueError("Location reference must be capture or hazard")
    result.setdefault("location_reference", "capture")
    if result.get("severity", "medium") not in {"low", "medium", "high"}:
        raise ValueError("Severity must be low, medium or high")
    result.setdefault("severity", "medium")
    for key, maximum in (("title", 240), ("notes", 4000), ("address", 500), ("hazard_type", 120), ("municipal_authority", 240)):
        result[key] = _text(result.get(key), key.replace("_", " ").title(), maximum)
    if result["mode"] == "manual" and not result["hazard_type"]:
        raise ValueError("Enter a hazard type for a manual report")
    track = result.get("gps_track")
    if isinstance(track, str):
        try:
            track = json.loads(track) if track.strip() else None
        except ValueError:
            raise ValueError("GPS track must be a JSON list of timestamped coordinates") from None
    if track is not None:
        if not isinstance(track, list) or not 2 <= len(track) <= 20_000:
            raise ValueError("GPS track must contain 2-20000 timestamped fixes")
        clean = []
        for point in track:
            if not isinstance(point, dict):
                raise ValueError("Every GPS fix must be an object")
            fix = {"time_s": number(point.get("time_s"), "GPS time in seconds", 0, MAX_VIDEO_SECONDS + 30),
                   "latitude": number(point.get("latitude"), "GPS latitude", -90, 90),
                   "longitude": number(point.get("longitude"), "GPS longitude", -180, 180),
                   "accuracy_m": None}
            if point.get("accuracy_m") is not None:
                fix["accuracy_m"] = number(point["accuracy_m"], "GPS accuracy in meters", 0, 10000)
            if clean and fix["time_s"] <= clean[-1]["time_s"]:
                raise ValueError("GPS times must be strictly increasing and relative to video start")
            clean.append(fix)
        result["gps_track"] = clean
        result["location_reference"] = "capture"
    else:
        result["gps_track"] = None
    lat, lon = result.get("latitude"), result.get("longitude")
    if lat not in (None, "") or lon not in (None, "") or track is None:
        result["latitude"] = number(lat, "Latitude", -90, 90)
        result["longitude"] = number(lon, "Longitude", -180, 180)
    else:
        result["latitude"], result["longitude"] = None, None
    accuracy = result.get("gps_accuracy_m")
    result["gps_accuracy_m"] = None if accuracy in (None, "") else number(accuracy, "GPS accuracy in meters", 0, 10000)
    return result


def position_at(values, time_s):
    """Interpolate capture GPS only, rejecting missing coverage and long gaps."""
    track = values.get("gps_track")
    if not track:
        return {"latitude": values.get("latitude"), "longitude": values.get("longitude"),
                "gps_accuracy_m": values.get("gps_accuracy_m")}
    times = [point["time_s"] for point in track]
    at = bisect.bisect_left(times, time_s)
    if at < len(track) and abs(times[at] - time_s) < 1e-7:
        return {"latitude": track[at]["latitude"], "longitude": track[at]["longitude"],
                "gps_accuracy_m": track[at]["accuracy_m"]}
    if at == 0 or at == len(track):
        return None
    left, right = track[at - 1], track[at]
    gap = right["time_s"] - left["time_s"]
    if gap > MAX_GPS_GAP_SECONDS:
        return None
    amount = (time_s - left["time_s"]) / gap
    longitude_delta = (right["longitude"] - left["longitude"] + 180) % 360 - 180
    accuracy = None if left["accuracy_m"] is None or right["accuracy_m"] is None else max(left["accuracy_m"], right["accuracy_m"])
    return {"latitude": left["latitude"] + amount * (right["latitude"] - left["latitude"]),
            "longitude": (left["longitude"] + amount * longitude_delta + 180) % 360 - 180,
            "gps_accuracy_m": accuracy}


def _resize(frame):
    height, width = frame.shape[:2]
    scale = min(1.0, 1280 / max(height, width))
    if scale < 1:
        return cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
    return frame


def _jpeg(frame, boxes=()):
    annotated = frame.copy()
    for box in boxes:
        x1, y1, x2, y2 = [int(value) for value in box["bbox"]]
        color = (50, 195, 240)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        label = f"{box['class_name']} {box['confidence']:.0%}"
        cv2.putText(annotated, label, (max(0, x1), max(18, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, .55, color, 2, cv2.LINE_AA)
    success, encoded = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 84])
    if not success:
        raise ValueError("Could not encode evidence image")
    return encoded.tobytes()


def _boxes(result, names):
    boxes = result.boxes
    if boxes is None:
        return []
    identities = boxes.id.cpu().tolist() if boxes.id is not None else [None] * len(boxes)
    output = []
    for bbox, label, confidence, identity in zip(boxes.xyxy.cpu().tolist(), boxes.cls.cpu().tolist(), boxes.conf.cpu().tolist(), identities):
        if len(bbox) != 4 or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in [*bbox, label, confidence]):
            continue
        if identity is not None and not math.isfinite(identity):
            continue
        output.append({"bbox": bbox, "class_name": str(names[int(label)]), "confidence": round(float(confidence), 4),
                       "track_id": int(identity) if identity is not None else None})
    return output


class SubmissionService:
    """One bounded inference job at a time, with restart-readable SQLite status."""
    def __init__(self, root, store, config_store, engine):
        self.root = Path(root).resolve()
        self.store, self.config_store, self.engine = store, config_store, engine
        self.upload_root = self.root / "roadwatch_data" / "uploads"
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self._busy = False
        self._persisted_at = {}
        self.stop_event = threading.Event()
        self.worker = None

    def stop(self):
        """Signal a server shutdown without abandoning an in-flight database write."""
        self.stop_event.set()
        worker = self.worker
        if worker and worker.is_alive():
            worker.join(timeout=2)

    shutdown = stop

    def _check_stop(self):
        if self.stop_event.is_set():
            raise RuntimeError("Processing interrupted by server shutdown. Saved evidence remains available.")

    def active(self):
        with self.lock:
            return self._busy

    def reserve(self):
        with self.engine.lock, self.lock:
            if self._busy or self.engine.active():
                raise RuntimeError("Stop detection and wait for the current upload before submitting another report")
            self._busy = True
            self.stop_event.clear()

    def release(self):
        with self.lock:
            self._busy = False

    def _job_update(self, job_id, force=False, **changes):
        with self.lock:
            job = self.jobs[job_id]
            job.update(changes)
            now = time.monotonic()
            if force or now - self._persisted_at.get(job_id, 0) >= 2:
                self._persisted_at[job_id] = now
                self.store.session_update(job_id, submission={key: value for key, value in job.items() if key != "hazards"})
            return copy.deepcopy(job)

    def job(self, job_id):
        with self.lock:
            if job_id in self.jobs:
                return copy.deepcopy(self.jobs[job_id])
        session = self.store.session(job_id)
        if not session:
            return None
        saved = session.get("metadata", {}).get("submission")
        if not saved:
            return None
        recovered = dict(saved)
        recovered["hazards"] = self.store.hazards(job_id)
        if recovered.get("status") in {"queued", "processing", "uploading"}:
            recovered.update(status="error", error="Processing was interrupted by a server restart. Submit the original media again.",
                             message="Upload processing was interrupted; saved evidence remains available.")
        return recovered

    def create(self, values, path=None, original_filename=None, kind=None):
        """Called after reserve and successful atomic publication of the upload."""
        job_id = uuid.uuid4().hex[:12]
        config = copy.deepcopy(self.config_store.value)
        self.store.start_session(job_id, "upload:" + (original_filename or "manual report"), config)
        job = {"id": job_id, "session_id": job_id, "media_id": job_id if path else None, "media_kind": kind,
               "status": "queued", "progress": 0, "hazards": [], "error": None, "message": "Report queued for local processing."}
        with self.lock:
            self.jobs[job_id] = job
        self.store.session_update(job_id, source_type="upload", source_path=str(path) if path else None,
                                  original_filename=original_filename, media_kind=kind, submission_id=job_id,
                                  submission=job, location_reference=values["location_reference"],
                                  gps_track=values.get("gps_track"), supplied_latitude=values.get("latitude"),
                                  supplied_longitude=values.get("longitude"))
        self.worker = threading.Thread(target=self._run, args=(job_id, values, path, kind, config), daemon=True)
        self.worker.start()
        return copy.deepcopy(job)

    def _hazard(self, job_id, key, box, values, time_s, kind):
        position = position_at(values, time_s)
        reference = values["location_reference"]
        if reference == "hazard":
            location_method = "user_supplied_hazard_location"
            location_note = "Hazard coordinates entered by the reporter; independently unverified."
        elif values.get("gps_track"):
            location_method = "interpolated_capture_location_proxy"
            location_note = "Interpolated camera position at this video timestamp, not a surveyed hazard center. No road-distance projection was applied."
        else:
            location_method = "fixed_capture_location_proxy"
            location_note = "Reporter-supplied capture location. A fixed coordinate applies to the whole upload and may differ from the hazard position; no road-distance projection was applied."
        if position is None:
            location_note = "No synchronized GPS track fix at this timestamp (outside coverage or gap over 3 seconds); hazard coordinates are unavailable."
        manual = values["mode"] == "manual"
        hazard = {"id": f"{job_id}:{key}", "session_id": job_id, "track_id": key,
                  "recording_id": None, "dataset_id": None, "source_type": "manual" if manual else "upload_" + str(kind),
                  "class_name": box["class_name"], "hazard_type": box["class_name"], "confidence": box.get("confidence"),
                  "bbox": box.get("bbox"), "observations": 1, "first_seen_s": time_s, "last_seen_s": time_s,
                  "latitude": position["latitude"] if position else None, "longitude": position["longitude"] if position else None,
                  "gps_accuracy_m": position.get("gps_accuracy_m") if position else None, "location_reference": reference,
                  "location_method": location_method, "location_note": location_note, "accuracy_note": location_note,
                  "distance_m": None, "provisional": True, "has_reference_labels": False, "match": None,
                  "status": "open", "severity": values["severity"], "title": values["title"] or box["class_name"],
                  "notes": values["notes"], "address": values["address"], "municipal_authority": values["municipal_authority"],
                  "review_status": "manual_report" if manual else "unreviewed_prediction",
                  "model_path": None if manual else values.get("_model_path"),
                  "submission_id": job_id}
        return hazard

    def _save_observation(self, job_id, key, box, values, frame, index, time_s, kind, annotations):
        identity = f"{job_id}:{key}"
        previous = self.store.hazard(identity)
        item = self._hazard(job_id, key, box, values, time_s, kind)
        if previous:
            # Keep one record per tracker and the strongest class observation.
            item.update(first_seen_s=previous["first_seen_s"], observations=previous["observations"] + 1)
            if previous.get("confidence", -1) is not None and previous.get("confidence", -1) > (box.get("confidence") or -1):
                item.update(class_name=previous["class_name"], hazard_type=previous["hazard_type"], confidence=previous["confidence"])
            # Keep the first supported capture proxy for a moving object. Averaging
            # vehicle locations would pretend to refine the physical hazard center.
            if previous.get("latitude") is not None:
                for field in ("latitude", "longitude", "gps_accuracy_m", "location_note", "accuracy_note"):
                    item[field] = previous.get(field)
        self.store.save_hazard(job_id, item)
        if len(self.store.evidence(identity)) < 10:
            image = _jpeg(frame, annotations)
            self.store.save_evidence(identity, image, {"frame_index": index, "time_s": time_s, "bbox": box.get("bbox"),
                                                      "class_name": box["class_name"], "confidence": box.get("confidence"),
                                                      "width": frame.shape[1], "height": frame.shape[0],
                                                      "source": "manual_upload" if values["mode"] == "manual" else "yolo_upload"})

    def _model(self, config):
        from ultralytics import YOLO
        model_path = Path(config["detection"]["model_path"])
        model_path = (self.root / model_path).resolve() if not model_path.is_absolute() else model_path.resolve()
        if not model_path.is_relative_to(self.root) or not model_path.is_file():
            raise ValueError("Choose an existing YOLO model inside the project folder")
        return YOLO(str(model_path))

    def _run(self, job_id, values, path, kind, config):
        values = {**values, "_model_path": config["detection"]["model_path"]}
        cap = None
        terminal = {"status": "error", "error": "Processing interrupted", "message": "Submission processing was interrupted."}
        try:
            self._check_stop()
            self._job_update(job_id, force=True, status="processing", message="Processing locally; evidence is capped at 10 frames per hazard.")
            manual = values["mode"] == "manual"
            if path is None:
                box = {"class_name": values["hazard_type"], "confidence": None, "bbox": None}
                item = self._hazard(job_id, "manual", box, values, 0, None)
                self.store.save_hazard(job_id, item)
                with self.lock:
                    self.jobs[job_id]["hazards"] = self.store.hazards(job_id)
            elif kind == "photo":
                with Image.open(path) as header:
                    if header.width * header.height > MAX_PHOTO_PIXELS:
                        raise ValueError("Use a valid image with at most 24 megapixels")
                data = np.fromfile(path, dtype=np.uint8)
                frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
                if frame is None or frame.shape[0] * frame.shape[1] > MAX_PHOTO_PIXELS:
                    raise ValueError("Use a valid image with at most 24 megapixels")
                frame = _resize(frame)
                if manual:
                    boxes = [{"class_name": values["hazard_type"], "confidence": None, "bbox": None}]
                else:
                    model = self._model(config)
                    settings = config["detection"]
                    kwargs = dict(conf=settings["confidence"], imgsz=settings["imgsz"], verbose=False)
                    if settings["device"] != "auto":
                        kwargs["device"] = settings["device"]
                    boxes = _boxes(model.predict(frame, **kwargs)[0], model.names)
                for index, box in enumerate(boxes):
                    self._check_stop()
                    self._save_observation(job_id, str(index + 1), box, values, frame, 0, 0, kind, [] if manual else boxes)
                self.store.session_update(job_id, duration_s=0, frame_count=1, frame_width=frame.shape[1], frame_height=frame.shape[0])
            else:
                cap = cv2.VideoCapture(str(path))
                if not cap.isOpened():
                    raise ValueError("Could not decode this video. Use an MP4, MOV, AVI, MKV or WebM clip.")
                fps = cap.get(cv2.CAP_PROP_FPS)
                if not math.isfinite(fps) or not 0 < fps <= 240:
                    raise ValueError("Video has no usable frame rate")
                width, height = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
                if width > 0 and height > 0 and width * height > MAX_PHOTO_PIXELS:
                    raise ValueError("Use video frames with at most 24 megapixels")
                total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                total = int(total) if math.isfinite(total) and total > 0 else None
                duration = total / fps if total else None
                if duration and duration > MAX_VIDEO_SECONDS + .1:
                    raise ValueError("Use a video no longer than five minutes")
                stride = max(config["detection"]["frame_stride"], math.ceil((total or MAX_VIDEO_SECONDS * fps) / MAX_VIDEO_INFERENCES))
                self.store.session_update(job_id, duration_s=duration, frame_count=total, native_fps=fps,
                                          sample_stride=stride, max_inference_frames=MAX_VIDEO_INFERENCES,
                                          frame_width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), frame_height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
                model = None if manual else self._model(config)
                index, sampled = -1, 0
                previous_time = None
                while cap.grab():
                    self._check_stop()
                    index += 1
                    if index / fps > MAX_VIDEO_SECONDS or sampled >= MAX_VIDEO_INFERENCES:
                        break
                    if index % stride:
                        continue
                    ok, frame = cap.retrieve()
                    if not ok:
                        raise ValueError("A sampled video frame could not be decoded")
                    frame = _resize(frame)
                    milliseconds = cap.get(cv2.CAP_PROP_POS_MSEC)
                    pts = milliseconds / 1000 if math.isfinite(milliseconds) and milliseconds >= 0 else None
                    time_s = pts if pts is not None and (previous_time is None or pts > previous_time) else index / fps
                    time_s = time_s if previous_time is None else max(time_s, previous_time + 1e-6)
                    previous_time = time_s
                    if time_s > MAX_VIDEO_SECONDS:
                        break
                    if manual:
                        box = {"class_name": values["hazard_type"], "confidence": None, "bbox": None}
                        self._save_observation(job_id, "manual", box, values, frame, index, time_s, kind, [])
                        break
                    settings = config["detection"]
                    kwargs = dict(persist=True, tracker="bytetrack.yaml", conf=settings["confidence"], imgsz=settings["imgsz"], verbose=False)
                    if settings["device"] != "auto":
                        kwargs["device"] = settings["device"]
                    boxes = _boxes(model.track(frame, **kwargs)[0], model.names)
                    for box in boxes:
                        if box["track_id"] is not None:
                            self._save_observation(job_id, str(box["track_id"]), box, values, frame, index, time_s, kind, boxes)
                    sampled += 1
                    self._job_update(job_id, progress=min(99, round(100 * (index + 1) / total)) if total else min(99, round(100 * time_s / MAX_VIDEO_SECONDS)),
                                     processed_frames=sampled, sample_stride=stride)
                if index < 0:
                    raise ValueError("This video contains no decodable frames")
                self.store.session_update(job_id, sampled_frames=sampled if not manual else 1,
                                          processed_duration_s=min(MAX_VIDEO_SECONDS, max(0, index / fps)))
            hazards = self.store.hazards(job_id)
            message = (f"Saved {len(hazards)} hazard report{'s' if len(hazards) != 1 else ''}; up to 10 evidence frames per hazard."
                       if hazards else "No supported hazards were detected. No hazard report was invented; try another image or submit a manual report.")
            terminal = {"status": "completed", "progress": 100, "hazards": hazards, "message": message}
            with self.lock:
                while len(self.jobs) > 100:
                    oldest = next(iter(self.jobs))
                    if oldest == job_id:
                        break
                    self.jobs.pop(oldest)
                    self._persisted_at.pop(oldest, None)
        except Exception as exc:
            terminal = {"status": "error", "error": str(exc), "message": "Submission processing failed; any saved evidence remains in the hazard library."}
        finally:
            if cap:
                cap.release()
            try:
                self.store.finish_session(job_id, terminal["status"])
            finally:
                # Completion and availability are published together; the next
                # click cannot observe completed while the GPU remains reserved.
                with self.lock:
                    self._busy = False
                    self._job_update(job_id, force=True, **terminal)


def make_submission_router(service):
    router = APIRouter()

    @router.post("/api/submissions", status_code=202)
    async def submit(file: UploadFile | None = File(None), mode: str = Form("detect"),
                     latitude: str | None = Form(None), longitude: str | None = Form(None),
                     location_reference: str = Form("capture"), hazard_type: str = Form(""),
                     title: str = Form(""), notes: str = Form(""), address: str = Form(""),
                     severity: str = Form("medium"), municipal_authority: str = Form(""),
                     gps_accuracy_m: str | None = Form(None), gps_track: str = Form("")):
        path = None
        reserved = False
        try:
            values = validate_submission({"mode": mode, "latitude": latitude, "longitude": longitude,
                                          "location_reference": location_reference, "hazard_type": hazard_type,
                                          "title": title, "notes": notes, "address": address, "severity": severity,
                                          "municipal_authority": municipal_authority, "gps_accuracy_m": gps_accuracy_m,
                                          "gps_track": gps_track})
            if mode == "detect" and file is None:
                raise ValueError("Choose a photo or video for detection, or select manual report")
            kind, original = None, None
            if file is not None and file.filename:
                original = Path(file.filename.replace("\\", "/")).name
                original = _text(original, "Filename", 255)
                suffix = Path(original).suffix.lower()
                if suffix in IMAGE_EXTENSIONS:
                    kind, limit = "photo", MAX_PHOTO_BYTES
                elif suffix in VIDEO_EXTENSIONS:
                    kind, limit = "video", MAX_UPLOAD_BYTES
                else:
                    raise ValueError("Choose a JPG, PNG, WebP, BMP image or MP4, MOV, AVI, MKV, WebM video")
                if kind == "photo" and values.get("gps_track"):
                    raise ValueError("A GPS track is for video. Enter one coordinate for a photo.")
            elif file is not None:
                file = None
            if mode == "detect" and file is None:
                raise ValueError("Choose a nonempty photo or video file")
            service.reserve()
            reserved = True
            if file is not None:
                path = service.upload_root / (uuid.uuid4().hex + suffix)
                temporary = path.with_suffix(path.suffix + ".partial")
                received = 0
                try:
                    with temporary.open("wb") as output:
                        while block := await file.read(1024 * 1024):
                            received += len(block)
                            if received > limit:
                                raise HTTPException(413, f"Use a {'photo up to 25 MB' if kind == 'photo' else 'video up to 250 MB'}")
                            await asyncio.to_thread(output.write, block)
                    if not received:
                        raise ValueError("The uploaded file is empty")
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            job = service.create(values, path, original, kind)
            reserved = False  # The worker now owns the reservation.
            return job
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            if file:
                await file.close()
            if reserved:
                service.release()
                if path:
                    path.unlink(missing_ok=True)

    @router.get("/api/submissions/{job_id}")
    def status(job_id: str):
        result = service.job(job_id)
        if result is None:
            raise HTTPException(404, "Submission not found")
        return result

    return router
