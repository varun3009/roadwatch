"""On-demand annotated replay with persistent, idempotent frame observations.

Only prediction metadata is cached for every visited frame. JPEG evidence stays
in the shared SQLite evidence store, with its hard ten-frame limit per hazard.
"""
from __future__ import annotations

import base64
import bisect
import copy
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException

from .comma_geometry import project_comma_pixel
from .config import effective_calibration, number
from .datasets import normalize_hazard
from .engine import HazardAccumulator
from .geometry import calibration_from_dict, locate_hazard, project_pixel
from .submissions import _boxes, position_at


LEASE_SECONDS = 60
ASSOCIATION_SECONDS = 1.25
MINIMUM_IOU = .25


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _iou(left, right):
    x1, y1 = max(left[0], right[0]), max(left[1], right[1])
    x2, y2 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    union = ((left[2] - left[0]) * (left[3] - left[1])
             + (right[2] - right[0]) * (right[3] - right[1]) - intersection)
    return intersection / union if union > 0 else 0


class _TransactionStore:
    """Let the existing accumulator share the frame-cache writer transaction."""
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        yield self.connection

    def save_hazard(self, session_id, event):
        self.connection.execute(
            "INSERT INTO hazards(id,session_id,payload,created_at,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=CURRENT_TIMESTAMP",
            (event["id"], session_id, _json(event)))


class ReplayDetectionService:
    def __init__(self, root, catalog, store, config_store, engine, submission_service, replay_service):
        self.root = Path(root).resolve()
        self.catalog, self.store, self.config_store = catalog, store, config_store
        self.engine, self.gate, self.replay = engine, submission_service, replay_service
        self.lock = threading.RLock()
        self.owner = None
        self.last_used = 0
        self.model = None
        self.model_key = None
        self.stopped = threading.Event()
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS replay_analyses (
                    id TEXT PRIMARY KEY, cache_key TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    config TEXT NOT NULL, context TEXT NOT NULL, next_track INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS replay_frames (
                    analysis_id TEXT NOT NULL REFERENCES replay_analyses(id), frame_index INTEGER NOT NULL,
                    time_s REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(analysis_id,frame_index));
                CREATE INDEX IF NOT EXISTS replay_frame_time_idx ON replay_frames(analysis_id,time_s);
            """)
        self.monitor = threading.Thread(target=self._monitor, daemon=True)
        self.monitor.start()

    def _monitor(self):
        while not self.stopped.wait(5):
            with self.lock:
                if self.owner and time.monotonic() - self.last_used >= LEASE_SECONDS:
                    self._release()

    def _release(self):
        owner = self.owner
        self.owner, self.model, self.model_key = None, None, None
        if owner:
            self.gate.release()
            row = self._analysis(owner)
            if row:
                self.store.finish_session(row["session_id"], "paused")

    def active(self):
        with self.lock:
            if self.owner and time.monotonic() - self.last_used >= LEASE_SECONDS:
                self._release()
            return self.owner is not None

    def stop(self):
        self.stopped.set()
        with self.lock:
            self._release()

    close = stop

    def close_analysis(self, analysis_id):
        with self.lock:
            if self.owner == analysis_id:
                self._release()
            return {"analysis_id": analysis_id, "closed": True, "message": "Replay paused; detections and evidence remain saved."}

    def _analysis(self, analysis_id):
        if not isinstance(analysis_id, str) or not 1 <= len(analysis_id) <= 80:
            return None
        with self.store.connect() as db:
            row = db.execute("SELECT id,session_id,source_id,config,context,next_track FROM replay_analyses WHERE id=?", (analysis_id,)).fetchone()
        return ({"id": row[0], "session_id": row[1], "source_id": row[2], "config": json.loads(row[3]),
                 "context": json.loads(row[4]), "next_track": row[5]} if row else None)

    def _claim(self, analysis_id):
        if self.owner and time.monotonic() - self.last_used >= LEASE_SECONDS:
            self._release()
        if self.owner and self.owner != analysis_id:
            self._release()
        if not self.owner:
            self.gate.reserve()
            self.owner = analysis_id
        self.last_used = time.monotonic()

    @contextmanager
    def _processing(self, analysis_id):
        # Reserve the shared GPU gate under the engine lock, then release that
        # lock before decoding/inference. WebSocket snapshots must stay live
        # while YOLO warms up. Our own lock protects the complete frame operation.
        self.lock.acquire()
        try:
            with self.engine.lock:
                analysis = self._analysis(analysis_id)
                if analysis is None:
                    raise ValueError("Replay analysis was not found; select the video again")
                if self.stopped.is_set():
                    raise RuntimeError("Replay service is stopping")
                self._claim(analysis_id)
            yield analysis
        finally:
            self.lock.release()

    def _source_context(self, source_id):
        record = self.replay.resolve(source_id)
        path = record["path"]
        config = copy.deepcopy(self.config_store.value)
        recording_id = record.get("recording_id")
        if recording_id and str(recording_id).startswith("comma2k19-"):
            config["calibration"] = effective_calibration(config, self.catalog.calibration_for(recording_id))
            config["sync_offset_s"] = 0
        session = self.store.session(record.get("session_id")) if record.get("session_id") else None
        metadata = (session or {}).get("metadata", {})
        origin = metadata.get("origin_source_type") or metadata.get("source_type")
        upload = record.get("kind") == "upload" or origin == "upload"
        location = {key: metadata.get(key) for key in ("location_reference", "gps_track", "supplied_latitude", "supplied_longitude", "gps_accuracy_m")}
        if upload and not location.get("gps_track") and location["gps_accuracy_m"] is None and session:
            existing = self.store.hazards(session["id"])
            location["gps_accuracy_m"] = next((item.get("gps_accuracy_m") for item in existing if item.get("gps_accuracy_m") is not None), None)
        model_path = Path(config["detection"]["model_path"])
        model_path = (self.root / model_path).resolve() if not model_path.is_absolute() else model_path.resolve()
        if not model_path.is_relative_to(self.root) or not model_path.is_file():
            raise ValueError("Choose an existing YOLO model inside the project folder")
        context = {"path": str(path), "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns,
                   "model_path": str(model_path), "model_size": model_path.stat().st_size, "model_mtime_ns": model_path.stat().st_mtime_ns,
                   "recording_id": recording_id, "dataset_id": record.get("dataset_id"), "upload": upload,
                   "location": location if upload else {}, "original_filename": metadata.get("original_filename"),
                   "has_labels": record.get("has_labels", False), "label": record.get("label") or "Saved video"}
        fingerprint = {key: value for key, value in context.items() if key not in {"label", "original_filename"}}
        fingerprint["config"] = config
        return context, config, hashlib.sha256(_json(fingerprint).encode()).hexdigest()

    def analyze(self, source_id):
        with self.lock, self.engine.lock:
            if self.stopped.is_set():
                raise RuntimeError("Replay service is stopping")
            context, config, key = self._source_context(source_id)
            # Do not create a session if another GPU workload already owns the gate.
            with self.store.connect() as db:
                existing = db.execute("SELECT id FROM replay_analyses WHERE cache_key=?", (key,)).fetchone()
            analysis_id = existing[0] if existing else uuid.uuid4().hex[:16]
            self._claim(analysis_id)
            try:
                if not existing:
                    session_id = uuid.uuid4().hex[:12]
                    self.store.start_session(session_id, "replay:" + (str(context["recording_id"]) if context["recording_id"] else context["label"]), config)
                    location = context["location"]
                    self.store.session_update(session_id, source_type="replay", origin_source_type="upload" if context["upload"] else "dataset" if context["recording_id"] else "file",
                                              source_path=context["path"], recording_id=context["recording_id"], replay_analysis_id=analysis_id,
                                              replay_source_id=source_id, original_filename=context.get("original_filename"), media_kind="video", **location)
                    with self.store.connect() as db:
                        db.execute("INSERT INTO replay_analyses(id,cache_key,session_id,source_id,config,context) VALUES(?,?,?,?,?,?)",
                                   (analysis_id, key, session_id, source_id, _json(config), _json(context)))
                analysis = self._analysis(analysis_id)
                self.store.finish_session(analysis["session_id"], "running")
                return self.summary(analysis_id)
            except BaseException:
                self._release()
                raise

    def summary(self, analysis_id):
        analysis = self._analysis(analysis_id)
        if analysis is None:
            raise ValueError("Replay analysis was not found; select the video again")
        info = {key: value for key, value in self.replay.info(analysis["source_id"]).items() if not key.startswith("_")}
        info.update(note="YOLO detections and bounding boxes are computed as frames are visited. Previously analyzed frames reuse saved detections without logging duplicates.",
                    detection_enabled=True, association_note="Nearby frame overlap associates predictions; large seeks or occlusions may produce separate hazard tracks.")
        with self.store.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM replay_frames WHERE analysis_id=?", (analysis_id,)).fetchone()[0]
        hazards = self.store.hazards(analysis["session_id"])
        return {"analysis_id": analysis_id, "session_id": analysis["session_id"], "info": info,
                "cached_frames": count, "hazards": hazards[-1000:], "hazard_count": len(hazards),
                "active": self.owner == analysis_id, "lease_seconds": LEASE_SECONDS}

    def _load_model(self, context):
        path = Path(context["model_path"])
        if not path.is_file() or path.stat().st_size != context["model_size"] or path.stat().st_mtime_ns != context["model_mtime_ns"]:
            raise ValueError("The YOLO model changed; select the video again to start a new analysis")
        key = (context["model_path"], context["model_size"], context["model_mtime_ns"])
        if self.model is None or self.model_key != key:
            from ultralytics import YOLO
            self.model = YOLO(context["model_path"])
            self.model_key = key
        return self.model

    def _location(self, box, width, height, time_s, analysis):
        context, config = analysis["context"], analysis["config"]
        record_id = context.get("recording_id")
        if context["upload"]:
            data = context["location"]
            values = {"latitude": data.get("supplied_latitude"), "longitude": data.get("supplied_longitude"),
                      "gps_accuracy_m": data.get("gps_accuracy_m"), "gps_track": data.get("gps_track")}
            location = position_at(values, time_s)
            reference = data.get("location_reference") or "capture"
            method = "user_supplied_hazard_location" if reference == "hazard" else "interpolated_capture_location_proxy" if values["gps_track"] else "fixed_capture_location_proxy"
            note = ("Reporter-supplied hazard coordinates; independently unverified." if reference == "hazard" else
                    "Camera/capture location used as an approximate hazard-location proxy, not a surveyed hazard center. No road-distance projection was applied.")
            if location is None:
                note = "GPS track has no synchronized fix here (outside coverage or gap over 3 seconds); coordinates are unavailable."
            return {"latitude": location.get("latitude") if location else None, "longitude": location.get("longitude") if location else None,
                    "gps_accuracy_m": location.get("gps_accuracy_m") if location else None, "distance_m": None,
                    "provisional": True, "location_reference": reference, "location_method": method, "location_note": note,
                    "accuracy_note": note, "method": method, "distance_kind": None}
        calibration = calibration_from_dict(config["calibration"])
        sensor_time = time_s + config["sync_offset_s"]
        mode = "dataset" if record_id else "manual" if config["pose"]["mode"] == "manual" else None
        pose = self.engine._pose(mode, record_id, sensor_time, config) if mode else None
        x1, y1, x2, y2 = box["bbox"]
        u = min(width - 1, max(0, (x1 + x2) / 2))
        v = min(height - 1, max(0, y2 if calibration.point_mode == "bottom_center" else (y1 + y2) / 2))
        comma = str(record_id).startswith("comma2k19-")
        projection = project_comma_pixel(u, v, width, height, calibration, pose or {}) if comma else project_pixel(u, v, width, height, calibration)
        location = projection if comma else locate_hazard(projection, pose, calibration) if pose else {}
        return {"latitude": location.get("latitude"), "longitude": location.get("longitude"),
                "gps_accuracy_m": pose.get("accuracy_m") if pose else None, "distance_m": projection.get("distance_m"),
                "forward_m": projection.get("forward_m"), "right_m": projection.get("right_m"),
                "provisional": not calibration.verified, "projection_reason": projection.get("reason") or location.get("reason"),
                "accuracy_note": projection.get("accuracy_note") if comma else None,
                "method": projection.get("method") or "calibrated_ray_flat_road",
                "distance_kind": "horizontal_from_camera" if comma else "horizontal_from_gps_antenna"}

    def _associate(self, db, analysis, detections, width, height, time_s):
        neighbors = db.execute("SELECT time_s,payload FROM replay_frames WHERE analysis_id=? AND time_s BETWEEN ? AND ? ORDER BY ABS(time_s-?) LIMIT 12",
                               (analysis["id"], time_s - ASSOCIATION_SECONDS, time_s + ASSOCIATION_SECONDS, time_s)).fetchall()
        candidates = {}
        for detection in detections:
            detection["normalized_bbox"] = [detection["bbox"][0] / width, detection["bbox"][1] / height,
                                             detection["bbox"][2] / width, detection["bbox"][3] / height]
        for neighbor_time, payload in neighbors:
            for previous in json.loads(payload)["detections"]:
                for index, detection in enumerate(detections):
                    if normalize_hazard(detection["class_name"]) != normalize_hazard(previous["class_name"]):
                        continue
                    overlap = _iou(detection["normalized_bbox"], previous["normalized_bbox"])
                    if overlap >= MINIMUM_IOU:
                        pair = (index, previous["track_id"])
                        score = overlap - .05 * abs(neighbor_time - time_s)
                        candidates[pair] = max(score, candidates.get(pair, -1))
        assigned_detections, assigned_tracks = set(), set()
        for (index, track), _ in sorted(candidates.items(), key=lambda item: item[1], reverse=True):
            if index not in assigned_detections and track not in assigned_tracks:
                detections[index]["track_id"] = track
                assigned_detections.add(index)
                assigned_tracks.add(track)
        next_track = analysis["next_track"]
        for index, detection in enumerate(detections):
            if index not in assigned_detections:
                detection["track_id"] = next_track
                next_track += 1
            detection["id"] = detection["track_id"]
        return next_track

    @staticmethod
    def _draw(frame, detections):
        image = frame.copy()
        for detection in detections:
            x1, y1, x2, y2 = map(int, detection["bbox"])
            color = (40, 210, 255)
            cv2.rectangle(image, (x1, y1), (x2, y2), color, 3, cv2.LINE_AA)
            label = f"{detection['class_name']} #{detection['track_id']} {detection['confidence']:.0%}"
            if detection.get("distance_m") is not None:
                label += f" | {detection['distance_m']:.1f} m"
            font = max(.65, min(1.0, image.shape[1] / 1400))
            (text_width, text_height), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font, 2)
            anchor = (max(0, min(x1, image.shape[1] - text_width - 8)), max(text_height + 8, y1))
            cv2.rectangle(image, (anchor[0], anchor[1] - text_height - 7), (min(image.shape[1] - 1, anchor[0] + text_width + 8), anchor[1] + baseline), (15, 25, 30), -1)
            cv2.putText(image, label, (anchor[0] + 4, anchor[1] - 3), cv2.FONT_HERSHEY_SIMPLEX, font, color, 2, cv2.LINE_AA)
        ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 86])
        if not ok:
            raise ValueError("Could not encode the annotated replay frame")
        return jpeg.tobytes()

    def process(self, analysis_id, time_s):
        time_s = number(time_s, "Replay time", 0, 100_000_000)
        with self._processing(analysis_id) as analysis:
            context, config = analysis["context"], analysis["config"]
            path = Path(context["path"])
            if not path.is_file() or path.stat().st_size != context["size"] or path.stat().st_mtime_ns != context["mtime_ns"]:
                raise ValueError("The video changed; select it again to start a new analysis")
            info = self.replay.info(analysis["source_id"])
            raw, actual_time = self.replay.frame(analysis["source_id"], time_s)
            frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError("The replay frame could not be decoded")
            height, width = frame.shape[:2]
            times = info.get("_times")
            index = max(0, bisect.bisect_right(times, actual_time) - 1) if times else int(round(actual_time * info["fps"]))
            with self.store.connect() as db:
                cached = db.execute("SELECT payload FROM replay_frames WHERE analysis_id=? AND frame_index=?", (analysis_id, index)).fetchone()
            model_info = {"path": config["detection"]["model_path"], "status": "Cached detections" if cached else "Loaded"}
            if cached:
                detections = json.loads(cached[0])["detections"]
            else:
                model = self._load_model(context)
                settings = config["detection"]
                kwargs = dict(conf=settings["confidence"], imgsz=settings["imgsz"], verbose=False, max_det=100)
                if settings["device"] != "auto":
                    kwargs["device"] = settings["device"]
                predictions = _boxes(model.predict(frame, **kwargs)[0], model.names)
                detections = []
                for prediction in predictions:
                    x1, y1, x2, y2 = prediction["bbox"]
                    prediction["bbox"] = [max(0, min(width - 1, x1)), max(0, min(height - 1, y1)), max(0, min(width - 1, x2)), max(0, min(height - 1, y2))]
                    if prediction["bbox"][2] <= prediction["bbox"][0] or prediction["bbox"][3] <= prediction["bbox"][1]:
                        continue
                    prediction.update(self._location(prediction, width, height, actual_time, analysis))
                    prediction.update(dataset_id=context.get("dataset_id"), has_reference_labels=context.get("has_labels", False),
                                      frame_index=index, frame_width=width, frame_height=height, source_type="replay_detection", video_time_s=actual_time)
                    detections.append(prediction)
                model_info["classes"] = list(model.names.values()) if isinstance(model.names, dict) else list(model.names)
                record_id = context.get("recording_id")
                refs = self.catalog.reference_events(record_id) if record_id else []
                refs += self.store.references()
                # One transaction commits each unique frame together with all
                # hazard observation counts. A retry after a crash cannot double-log.
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    next_track = self._associate(db, analysis, detections, width, height, actual_time)
                    transaction_store = _TransactionStore(db)
                    accumulator = HazardAccumulator(analysis["session_id"], transaction_store)
                    for detection in detections:
                        previous_row = db.execute("SELECT payload FROM hazards WHERE id=?", (f"{analysis['session_id']}:{detection['track_id']}",)).fetchone()
                        previous = json.loads(previous_row[0]) if previous_row else None
                        item = accumulator.update(detection, actual_time + config["sync_offset_s"], record_id, refs, settings)
                        item.update(source_type="replay_detection", replay_analysis_id=analysis_id, review_status="unreviewed_prediction",
                                    model_path=config["detection"]["model_path"], replay_source_id=analysis["source_id"],
                                    first_video_time_s=min(previous.get("first_video_time_s", actual_time), actual_time) if previous else actual_time,
                                    last_video_time_s=max(previous.get("last_video_time_s", actual_time), actual_time) if previous else actual_time)
                        if context["upload"]:
                            for field in ("location_reference", "location_method", "location_note", "accuracy_note"):
                                item[field] = detection.get(field)
                            item["ground_model"] = None
                            if previous and previous.get("latitude") is not None:
                                for field in ("latitude", "longitude", "gps_accuracy_m", "location_note", "accuracy_note"):
                                    item[field] = previous.get(field)
                        if previous:
                            item["first_seen_s"] = min(previous["first_seen_s"], item["first_seen_s"], actual_time + config["sync_offset_s"])
                            item["last_seen_s"] = max(previous["last_seen_s"], item["last_seen_s"])
                        transaction_store.save_hazard(analysis["session_id"], item)
                    db.execute("INSERT INTO replay_frames(analysis_id,frame_index,time_s,payload) VALUES(?,?,?,?)",
                               (analysis_id, index, actual_time, _json({"detections": detections, "width": width, "height": height})))
                    db.execute("UPDATE replay_analyses SET next_track=? WHERE id=?", (next_track, analysis_id))
            jpeg = self._draw(frame, detections)
            # This also repairs a crash between frame metadata and evidence save.
            # Exact repeats are deduplicated by SQLite's unique hazard/frame key.
            for detection in detections:
                hazard_id = f"{analysis['session_id']}:{detection['track_id']}"
                evidence = self.store.evidence(hazard_id)
                if len(evidence) >= 10 or any(abs(item.get("time_s", -1000) - actual_time) < .5 for item in evidence):
                    continue
                self.store.save_evidence(hazard_id, jpeg, {"frame_index": index, "time_s": actual_time, "width": width, "height": height,
                                                          "bbox": detection["bbox"], "class_name": detection["class_name"], "confidence": detection["confidence"],
                                                          "latitude": detection.get("latitude"), "longitude": detection.get("longitude"), "source": "yolo_annotated_replay"})
            hazards = self.store.hazards(analysis["session_id"])
            self.last_used = time.monotonic()
            self.store.finish_session(analysis["session_id"], "running")
            return {"analysis_id": analysis_id, "session_id": analysis["session_id"], "frame_index": index, "time_s": actual_time,
                    "image_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
                    "detections": detections, "hazards": hazards[:1000], "hazard_count": len(hazards), "cached": bool(cached),
                    "evidence_count": sum(hazard.get("evidence_count", 0) for hazard in hazards), "model": model_info}


def make_replay_detection_router(service):
    router = APIRouter()

    @router.post("/api/replay/analyze")
    def analyze(value: dict):
        try:
            return service.analyze(value.get("source_id"))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, TypeError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/api/replay/process")
    def process(value: dict):
        try:
            return service.process(value.get("analysis_id"), value.get("time_s", 0))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, TypeError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/api/replay/close")
    def close(value: dict):
        return service.close_analysis(value.get("analysis_id"))

    @router.get("/api/replay/analysis/{analysis_id}")
    def analysis(analysis_id: str):
        try:
            return service.summary(analysis_id)
        except (ValueError, TypeError, OSError) as exc:
            raise HTTPException(404, str(exc)) from exc

    return router
