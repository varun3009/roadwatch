"""Recover real source photographs for hazards saved before evidence capture.

Recovered frames are explicitly reconstructed. A current-model overlay is not
the original tracker output and never changes a hazard's historical class/GPS.
"""
from __future__ import annotations

import bisect
import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import uuid

import cv2
import numpy as np
from PIL import Image
from fastapi import APIRouter, HTTPException

from .datasets import normalize_hazard
from .engine import _resize_for_processing


PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MAX_RECOVERY_HAZARDS = 100
MAX_RECOVERY_FRAMES = 3


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _video_bounds(hazard, session):
    explicit_video_clock = _finite(hazard.get("first_video_time_s")) or _finite(hazard.get("last_video_time_s"))
    first = hazard.get("first_video_time_s") if explicit_video_clock else hazard.get("first_seen_s")
    last = hazard.get("last_video_time_s") if explicit_video_clock else hazard.get("last_seen_s")
    first = first if _finite(first) else last
    last = last if _finite(last) else first
    if not _finite(first):
        raise ValueError("No observation timestamp was saved for this video hazard")
    metadata = (session or {}).get("metadata") or {}
    original = str((session or {}).get("source") or "")
    recording_id = str(hazard.get("recording_id") or metadata.get("recording_id") or original.removeprefix("dataset:"))
    is_upload = metadata.get("source_type") == "upload" or str(hazard.get("source_type", "")).startswith(("upload", "manual"))
    is_roadsens = (hazard.get("dataset_id") == "roadsens" or original.startswith("dataset:")) and not recording_id.startswith("comma2k19-")
    offset = ((session or {}).get("config") or {}).get("sync_offset_s", 0) if is_roadsens and not is_upload and not explicit_video_clock else 0
    offset = offset if _finite(offset) else 0
    return sorted((first - offset, last - offset))


def recovery_times(hazard, session, duration_s):
    """Return source-video times, undoing RoadSens's saved sensor-clock offset."""
    if not _finite(duration_s) or duration_s <= 0:
        raise ValueError("The original video has no usable duration")
    first, last = _video_bounds(hazard, session)
    upper = max(0, duration_s - 1e-6)
    values = (first, (first + last) / 2, last) if last - first > .05 else (first,)
    return sorted({round(min(upper, max(0, value)), 6) for value in values})


class EvidenceRecoveryService:
    def __init__(self, root, store, catalog, replay_service, config_store, engine,
                 submission_service, replay_detection_optional=None):
        self.root, self.store, self.catalog = Path(root).resolve(), store, catalog
        self.replay = replay_service
        self.config_store, self.engine, self.submissions = config_store, engine, submission_service
        self.replay_detection = replay_detection_optional
        self.lock = threading.RLock()
        self.worker = None
        self.stop_event = threading.Event()
        self.current_job = None
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS evidence_recovery_jobs (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS evidence_recovery_items (
                    hazard_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
            ''')
            rows = db.execute("SELECT id,payload FROM evidence_recovery_jobs").fetchall()
            for identity, payload in rows:
                value = json.loads(payload)
                if value.get("status") in {"queued", "processing"}:
                    value.update(status="error", message="Evidence recovery was interrupted by a server restart. Saved images remain available; recovery can be retried.")
                    db.execute("UPDATE evidence_recovery_jobs SET payload=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(value, allow_nan=False), identity))

    def active(self):
        with self.lock:
            return bool(self.current_job and self.current_job.get("status") in {"queued", "processing"})

    def stop(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=2)

    def _check_stop(self):
        if self.stop_event.is_set():
            raise RuntimeError("Evidence recovery was interrupted by server shutdown; saved images remain available")

    def _job_update(self, job, **changes):
        with self.lock:
            job.update(changes)
            job["updated_at"] = datetime.now(timezone.utc).isoformat()
            snapshot = copy.deepcopy(job)
        with self.store.connect() as db:
            db.execute("INSERT INTO evidence_recovery_jobs(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=CURRENT_TIMESTAMP",
                       (job["id"], json.dumps(snapshot, allow_nan=False)))
        return snapshot

    def _item(self, hazard_id, **value):
        item = {"hazard_id": hazard_id, **value}
        with self.store.connect() as db:
            db.execute("INSERT INTO evidence_recovery_items(hazard_id,payload) VALUES(?,?) ON CONFLICT(hazard_id) DO UPDATE SET payload=excluded.payload,updated_at=CURRENT_TIMESTAMP",
                       (hazard_id, json.dumps(item, allow_nan=False)))
        return item

    def job(self, identity):
        with self.store.connect() as db:
            row = db.execute("SELECT payload FROM evidence_recovery_jobs WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def _source(self, hazard):
        session_id = hazard.get("session_id")
        session = self.store.session(session_id) if session_id else None
        metadata = (session or {}).get("metadata") or {}
        source_path = metadata.get("source_path")
        photo = metadata.get("media_kind") == "photo" or (source_path and Path(source_path).suffix.lower() in PHOTO_SUFFIXES)
        if photo:
            path = Path(source_path or "")
            path = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
            if not path.is_relative_to(self.root) or not path.is_file() or path.suffix.lower() not in PHOTO_SUFFIXES:
                raise ValueError("The original uploaded photo is missing or outside the project")
            if path.stat().st_size > 25 * 1024 * 1024:
                raise ValueError("The original photo exceeds the supported recovery size")
            return {"kind": "photo", "path": path, "session": session, "source_id": "session:" + str(session_id)}
        source_id = "session:" + str(session_id) if session else "dataset:" + str(hazard.get("recording_id") or "")
        if not session and not hazard.get("recording_id"):
            raise ValueError("No saved session or dataset recording identifies the original media")
        record = self.replay.resolve(source_id)
        path = Path(record["path"]).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("The original video is missing or outside the project")
        return {"kind": "video", "path": path, "source_id": source_id, "session": session}

    def status(self):
        hazards = self.store.hazards()
        with self.store.connect() as db:
            attempts = {identity: json.loads(payload) for identity, payload in db.execute("SELECT hazard_id,payload FROM evidence_recovery_items")}
        missing, items = [], []
        for hazard in hazards:
            if hazard.get("evidence_count", 0):
                continue
            missing.append(hazard)
            previous = attempts.get(hazard["id"], {})
            try:
                source = self._source(hazard)
                if source["kind"] == "video" and not any(_finite(hazard.get(name)) for name in ("first_video_time_s", "last_video_time_s", "first_seen_s", "last_seen_s")):
                    raise ValueError("No observation timestamp was saved for this video hazard")
                item = {"hazard_id": hazard["id"], "recoverable": True, "status": previous.get("status", "missing"),
                        "reason": previous.get("reason") or "The original local media can be decoded to recover photographs"}
            except (ValueError, OSError, TypeError) as exc:
                item = {"hazard_id": hazard["id"], "recoverable": False, "status": "unavailable", "reason": str(exc)}
            items.append(item)
        with self.lock:
            active_job = copy.deepcopy(self.current_job) if self.current_job else None
        recoverable = sum(item["recoverable"] for item in items)
        return {"total": len(hazards), "missing": len(missing), "with_evidence": len(hazards) - len(missing),
                "recoverable": recoverable, "unavailable": len(missing) - recoverable,
                "active": self.active(), "job": active_job, "items": items}

    def create(self, hazard_ids=None, limit=20):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RECOVERY_HAZARDS:
            raise ValueError("Recover 1-100 hazards per job")
        if hazard_ids is not None:
            if not isinstance(hazard_ids, list) or not hazard_ids or len(hazard_ids) > MAX_RECOVERY_HAZARDS or any(not isinstance(identity, str) or not identity or len(identity) > 250 for identity in hazard_ids):
                raise ValueError("Supply a list of 1-100 saved hazard IDs")
            identities = list(dict.fromkeys(hazard_ids))[:limit]
            for identity in identities:
                if self.store.hazard(identity) is None:
                    raise ValueError("One of the requested hazards does not exist")
        else:
            identities = [item["id"] for item in self.store.hazards() if not item.get("evidence_count", 0)][:limit]
        if not identities:
            return {"id": None, "status": "completed", "progress": 100, "total": 0, "processed": 0,
                    "recovered": 0, "failed": 0, "skipped": 0, "results": [], "message": "Every saved hazard already has evidence photographs."}
        with self.lock:
            if self.active():
                raise RuntimeError("Evidence recovery is already running")
            # The same reservation is used by live/upload/replay inference.
            self.submissions.reserve()
            self.stop_event.clear()
            job = {"id": "recover-" + uuid.uuid4().hex[:16], "status": "queued", "progress": 0,
                   "total": len(identities), "processed": 0, "recovered": 0, "failed": 0,
                   "skipped": 0, "results": [], "message": "Preparing source photographs for missing hazard evidence.",
                   "hazard_ids": identities, "created_at": datetime.now(timezone.utc).isoformat()}
            try:
                self.current_job = job
                self._job_update(job)
                config = copy.deepcopy(self.config_store.value)
                self.worker = threading.Thread(target=self._run, args=(job, identities, config), daemon=True)
                self.worker.start()
            except BaseException:
                self.current_job = None
                self.submissions.release()
                raise
            return copy.deepcopy(job)

    def _make_model(self, config):
        from ultralytics import YOLO
        path = Path(config["detection"]["model_path"])
        path = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("A valid local YOLO model is unavailable; source frames will have no new model overlay")
        return YOLO(str(path))

    def _predictions(self, model, frame, hazard, config):
        settings = config["detection"]
        kwargs = {"conf": settings["confidence"], "imgsz": settings["imgsz"], "verbose": False}
        if settings["device"] != "auto":
            kwargs["device"] = settings["device"]
        result = model.predict(frame, **kwargs)[0]
        boxes = result.boxes
        if boxes is None:
            return []
        family = normalize_hazard(hazard.get("class_name") or hazard.get("hazard_type"))
        predictions = []
        for bbox, label, confidence in zip(boxes.xyxy.cpu().tolist(), boxes.cls.cpu().tolist(), boxes.conf.cpu().tolist()):
            if len(bbox) != 4 or not all(_finite(value) for value in [*bbox, label, confidence]):
                continue
            name = str(model.names[int(label)])
            if normalize_hazard(name) == family:
                predictions.append({"bbox": bbox, "class_name": name, "confidence": float(confidence), "origin": "current_model_prediction"})
        return sorted(predictions, key=lambda value: value["confidence"], reverse=True)[:3]

    @staticmethod
    def _stored_box(hazard, time_s, last_observation_time, shape):
        bbox = hazard.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or not all(_finite(value) for value in bbox):
            return None
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return None
        # The persisted hazard box is the last observation, not every frame's
        # box. Reusing it at an earlier timestamp would invent tracker evidence.
        if abs(time_s - last_observation_time) > .11:
            return None
        bbox = list(bbox)
        height, width = shape[:2]
        old_width, old_height = hazard.get("frame_width"), hazard.get("frame_height")
        if _finite(old_width) and _finite(old_height) and old_width > 0 and old_height > 0:
            bbox = [bbox[0] * width / old_width, bbox[1] * height / old_height,
                    bbox[2] * width / old_width, bbox[3] * height / old_height]
        if bbox[0] < 0 or bbox[1] < 0 or bbox[2] > width + 1 or bbox[3] > height + 1:
            return None
        return {"bbox": bbox, "class_name": hazard.get("class_name") or hazard.get("hazard_type"),
                "confidence": hazard.get("confidence"), "origin": "recovered_original_bbox"}

    @staticmethod
    def _annotate(frame, overlays):
        frame = frame.copy()
        height, width = frame.shape[:2]
        for overlay in overlays:
            x1, y1, x2, y2 = overlay["bbox"]
            x1, x2 = max(0, min(width - 1, int(x1))), max(0, min(width - 1, int(x2)))
            y1, y2 = max(0, min(height - 1, int(y1))), max(0, min(height - 1, int(y2)))
            cv2.rectangle(frame, (x1, y1), (x2, y2), (50, 195, 240), 2)
            prefix = "Current model: " if overlay["origin"] == "current_model_prediction" else "Stored box: "
            cv2.putText(frame, prefix + str(overlay["class_name"]), (x1, max(18, y1 - 7)), cv2.FONT_HERSHEY_SIMPLEX, .5, (50, 195, 240), 2, cv2.LINE_AA)
        origin = overlays[0]["origin"] if overlays else "no_original_bbox"
        note = ("Recovered source frame | current predictions, not original tracking" if origin == "current_model_prediction"
                else "Recovered source frame | stored last-observation box" if origin == "recovered_original_bbox"
                else "Recovered source frame | original detection box unavailable")
        cv2.rectangle(frame, (0, max(0, height - 27)), (width, height), (15, 25, 30), -1)
        cv2.putText(frame, note, (8, max(14, height - 9)), cv2.FONT_HERSHEY_SIMPLEX, .45, (245, 245, 245), 1, cv2.LINE_AA)
        success, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 84])
        if not success:
            raise ValueError("Could not encode the recovered evidence photograph")
        return encoded.tobytes(), origin

    def _recover(self, hazard, config, model_state):
        if self.store.evidence(hazard["id"]):
            return {"hazard_id": hazard["id"], "status": "skipped", "reason": "Photographs already exist for this hazard", "saved_frames": 0}
        source = self._source(hazard)
        if source["kind"] == "photo":
            with Image.open(source["path"]) as image:
                if image.width * image.height > 24_000_000:
                    raise ValueError("The original photo exceeds 24 megapixels")
            frame = cv2.imdecode(np.fromfile(source["path"], dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError("The original uploaded photo could not be decoded")
            frames = [(0., 0, _resize_for_processing(frame))]
            times = [0.]
            last_observation_time = 0.
        else:
            info = self.replay.info(source["source_id"])
            times = recovery_times(hazard, source["session"], info["duration_s"])
            last_observation_time = _video_bounds(hazard, source["session"])[1]
            frames = []
            for requested in times[:MAX_RECOVERY_FRAMES]:
                self._check_stop()
                jpeg, actual = self.replay.frame(source["source_id"], requested)
                frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    raise ValueError("The original video frame could not be decoded")
                native_times = info.get("_times")
                index = max(0, bisect.bisect_right(native_times, actual + 1e-6) - 1) if native_times else max(0, round(actual * info["fps"]))
                frames.append((actual, index, frame))
        count = 0
        for actual, index, frame in frames:
            self._check_stop()
            stored = self._stored_box(hazard, actual, last_observation_time, frame.shape)
            overlays = [stored] if stored else []
            overlay_error = None
            if not overlays:
                if not model_state.get("attempted"):
                    model_state["attempted"] = True
                    try:
                        model_state["model"] = self._make_model(config)
                    except Exception as exc:
                        model_state["error"] = str(exc)
                if model_state.get("model") is not None:
                    try:
                        overlays = self._predictions(model_state["model"], frame, hazard, config)
                    except Exception as exc:
                        overlay_error = str(exc)
                else:
                    overlay_error = model_state.get("error")
            jpeg, origin = self._annotate(frame, overlays)
            metadata = {"frame_index": index, "time_s": actual, "width": frame.shape[1], "height": frame.shape[0],
                        "bbox": overlays[0]["bbox"] if overlays else None,
                        "class_name": hazard.get("class_name"), "confidence": hazard.get("confidence"),
                        "source": "recovered_source_frame", "source_id": source["source_id"],
                        "reconstructed": True, "overlay_origin": origin, "overlays": overlays,
                        "original_track_association_verified": False,
                        "note": "Recovered from original local media after detection; current-model boxes are reconstructed predictions, not original tracker output."}
            if overlay_error:
                metadata["overlay_error"] = overlay_error[:500]
            before = len(self.store.evidence(hazard["id"]))
            self.store.save_evidence(hazard["id"], jpeg, metadata)
            count += max(0, len(self.store.evidence(hazard["id"])) - before)
        if not count and not self.store.evidence(hazard["id"]):
            raise ValueError("No unique source frames could be saved for this hazard")
        return {"hazard_id": hazard["id"], "status": "recovered", "saved_frames": count,
                "reason": "Source photographs recovered; reconstructed overlays are identified in the evidence metadata"}

    def _run(self, job, identities, config):
        model_state = {}
        try:
            self._job_update(job, status="processing", message="Recovering original source photographs; missing detection boxes may be reconstructed with the current model.")
            for identity in identities:
                self._check_stop()
                try:
                    hazard = self.store.hazard(identity)
                    if hazard is None:
                        raise ValueError("Hazard no longer exists")
                    result = self._recover(hazard, config, model_state)
                except (ValueError, OSError, RuntimeError, cv2.error) as exc:
                    if self.stop_event.is_set():
                        raise
                    result = {"hazard_id": identity, "status": "failed", "saved_frames": 0, "reason": str(exc)}
                self._item(identity, **{key: value for key, value in result.items() if key != "hazard_id"})
                job["results"].append(result)
                job["processed"] += 1
                job["recovered"] += result["status"] == "recovered"
                job["failed"] += result["status"] == "failed"
                job["skipped"] += result["status"] == "skipped"
                self._job_update(job, progress=round(100 * job["processed"] / job["total"]),
                                 message=f"Recovered photographs for {job['recovered']} hazards; {job['failed']} unavailable, {job['skipped']} already had evidence.")
            self._job_update(job, status="completed", progress=100)
        except Exception as exc:
            self._job_update(job, status="error", message=str(exc))
        finally:
            model_state.clear()
            self.submissions.release()


def make_evidence_recovery_router(service):
    router = APIRouter()

    @router.get("/api/library/evidence-status")
    def status():
        return service.status()

    def start(value):
        if not isinstance(value, dict) or set(value) - {"hazard_ids", "limit"}:
            raise HTTPException(422, "Supply hazard_ids and/or a recovery limit")
        try:
            job = service.create(value.get("hazard_ids"), value.get("limit", 20))
            return {**job, "job": job}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/library/recover-evidence", status_code=202)
    def recover(value: dict):
        return start(value)

    @router.post("/api/library/{hazard_id}/recover-evidence", status_code=202)
    def recover_hazard(hazard_id: str):
        if service.store.hazard(hazard_id) is None:
            raise HTTPException(404, "Hazard not found")
        return start({"hazard_ids": [hazard_id], "limit": 1})

    @router.get("/api/library/evidence-jobs/{job_id}")
    def job(job_id: str):
        value = service.job(job_id)
        if value is None:
            raise HTTPException(404, "Evidence recovery job not found")
        return {**value, "job": value}

    return router
