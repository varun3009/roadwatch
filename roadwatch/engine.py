"""Bounded-memory YOLO worker, localization, tracking and reference association."""
from __future__ import annotations

import copy
from collections import OrderedDict
import json
import math
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

from .datasets import match_reference, normalize_hazard
from .geometry import calibration_from_dict, project_pixel, locate_hazard, haversine_m
from .comma_geometry import project_comma_pixel
from .config import effective_calibration


# Uniformly resize large source frames before inference/annotation/JPEG. This
# preserves the camera aspect ratio and scales geometry without a crop or pad.
PREVIEW_MAX_DIM = 1280
MAX_EVIDENCE_FRAMES = 10


def _resize_for_processing(frame):
    height, width = frame.shape[:2]
    scale = min(1.0, PREVIEW_MAX_DIM / max(height, width))
    if scale == 1.0:
        return frame
    return cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)


class HazardAccumulator:
    """Bounded LRU backed by SQLite; evicted track IDs retain their evidence."""
    def __init__(self, session_id, store, max_items=1000):
        if not isinstance(max_items, int) or max_items < 1:
            raise ValueError("max_items must be a positive integer")
        self.session_id = session_id
        self.store = store
        self.max_items = max_items
        self.items = OrderedDict()

    def update(self, detection, time_s, recording_id, references, settings):
        key = detection["track_id"]
        observed_class = detection["class_name"]
        observed_confidence = detection["confidence"]
        item = self.items.get(key)
        if item is None:
            identity = f"{self.session_id}:{key}"
            with self.store.connect() as db:
                saved = db.execute("SELECT payload FROM hazards WHERE id=? AND session_id=?", (identity, self.session_id)).fetchone()
            item = json.loads(saved[0]) if saved else None
        if item is None:
            item = {"id": f"{self.session_id}:{key}", "session_id": self.session_id,
                    "class_name": observed_class, "hazard_type": observed_class, "track_id": key,
                    "recording_id": recording_id, "first_seen_s": time_s,
                    "observations": 0, "located_observations": 0, "latitude": None,
                    "longitude": None, "match": None, "class_evidence": {}}
        self.items[key] = item
        self.items.move_to_end(key)
        evidence = item["class_evidence"].setdefault(observed_class, {"observations": 0, "max_confidence": 0.0})
        evidence["observations"] += 1
        evidence["max_confidence"] = max(evidence["max_confidence"], observed_confidence)
        previous_class = item["class_name"]
        # A tracker ID identifies one object even when noisy frame classifiers
        # disagree. Keep the label with the strongest individual observation;
        # equal confidence retains the previous stable label.
        previous_peak = item["class_evidence"][previous_class]["max_confidence"]
        if evidence["max_confidence"] > previous_peak:
            item["class_name"] = observed_class
        item["hazard_type"] = item["class_name"]
        item["class_ambiguous"] = len(item["class_evidence"]) > 1
        item["class_selection_method"] = "strongest_individual_confidence"
        if normalize_hazard(previous_class) != normalize_hazard(item["class_name"]):
            item["match"] = None
            item.pop("matched_at_s", None)
        item["observations"] += 1
        item["first_seen_s"] = min(item["first_seen_s"], time_s)
        item.update(last_seen_s=max(item.get("last_seen_s", time_s), time_s), confidence=item["class_evidence"][item["class_name"]]["max_confidence"],
                    latest_class_name=observed_class, latest_confidence=observed_confidence,
                    distance_m=detection.get("distance_m"), provisional=detection.get("provisional", True),
                    projection_reason=detection.get("projection_reason"), method=detection.get("method", "calibrated_ray_flat_road"),
                    distance_kind=detection.get("distance_kind", "horizontal_from_gps_antenna"), ground_model="flat_road",
                    dataset_id=detection.get("dataset_id"), has_reference_labels=detection.get("has_reference_labels"))
        if detection.get("bbox") is not None:
            item["bbox"] = detection["bbox"]
        for field in ("frame_width", "frame_height", "source_type", "frame_index"):
            if detection.get(field) is not None:
                item[field] = detection[field]
        if detection.get("video_time_s") is not None:
            video_time = detection["video_time_s"]
            item["first_video_time_s"] = min(item.get("first_video_time_s", video_time), video_time)
            item["last_video_time_s"] = max(item.get("last_video_time_s", video_time), video_time)
        lat, lon = detection.get("latitude"), detection.get("longitude")
        if _valid_coordinates(lat, lon):
            # Reject probable track-ID switches/poor views. Never average distant hazards.
            compatible = item["latitude"] is None or haversine_m(item["latitude"], item["longitude"], lat, lon) <= 12
            if compatible:
                n = item["located_observations"]
                weight = min(n, 19)
                item["latitude"] = lat if n == 0 else (weight * item["latitude"] + lat) / (weight + 1)
                # Circular interpolation handles the longitude wrap at +/-180.
                item["longitude"] = lon if n == 0 else (
                    (item["longitude"] + ((lon - item["longitude"] + 180) % 360 - 180) / (weight + 1) + 180) % 360 - 180
                )
                item["located_observations"] += 1
                accuracy = detection.get("gps_accuracy_m")
                if _finite_number(accuracy) and accuracy >= 0:
                    item["max_reported_gps_accuracy_m"] = max(item.get("max_reported_gps_accuracy_m", 0), accuracy)
                else:
                    item["unknown_accuracy_observations"] = item.get("unknown_accuracy_observations", 0) + 1
                item["gps_accuracy_m"] = None if item.get("unknown_accuracy_observations", 0) else item.get("max_reported_gps_accuracy_m")
                item["accuracy_note"] = detection.get("accuracy_note") or "GNSS reported accuracy only; calibration/range error unmeasured. Shared GPS bias is not averaged away."
            else:
                item["rejected_location_observations"] = item.get("rejected_location_observations", 0) + 1
        candidate = {**item, "time_s": time_s, "hazard_type": item["class_name"]}
        match = match_reference(candidate, references, radius_m=settings["match_radius_m"],
                                time_tolerance_s=settings["time_tolerance_s"], same_recording_only=False)
        # Keep supported evidence when subsequent frames leave the time window.
        def match_score(value):
            return sum(value.get(field) / max(limit, 1e-9) for field, limit in (
                ("distance_m", settings["match_radius_m"]),
                ("time_delta_s", settings["time_tolerance_s"]),
            ) if value.get(field) is not None)
        if match is not None and (item["match"] is None or match_score(match) < match_score(item["match"])):
            item["match"] = match
            item["matched_at_s"] = time_s
        detection["stable_class_name"] = item["class_name"]
        detection["class_ambiguous"] = item["class_ambiguous"]
        detection["match"] = item["match"] if normalize_hazard(observed_class) == normalize_hazard(item["class_name"]) else None
        self.store.save_hazard(self.session_id, item)
        while len(self.items) > self.max_items:
            self.items.popitem(last=False)
        return item

    def save_frame(self, detections, jpeg, frame_index, time_s, width, height):
        """Keep a spaced, bounded JPEG record for each confirmed tracker identity."""
        seen = set()
        for detection in detections:
            key = detection.get("track_id")
            if key is None or key in seen or key not in self.items:
                continue
            seen.add(key)
            item = self.items[key]
            if item.get("evidence_count", 0) >= MAX_EVIDENCE_FRAMES:
                continue
            previous = item.get("last_evidence_time_s")
            if previous is not None and time_s - previous < 0.5:
                continue
            saved = self.store.save_evidence(item["id"], jpeg, {
                "frame_index": frame_index, "time_s": time_s,
                "width": width, "height": height, "bbox": detection.get("bbox"),
                "class_name": detection.get("class_name"), "confidence": detection.get("confidence"),
                "latitude": detection.get("latitude"), "longitude": detection.get("longitude"),
                "provisional": detection.get("provisional", True), "source": "tracked_video_frame",
            }, max_frames=MAX_EVIDENCE_FRAMES)
            if saved:
                item["evidence_count"] = item.get("evidence_count", 0) + 1
                item["last_evidence_time_s"] = time_s
                item.setdefault("thumbnail_url", saved.get("url"))


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_coordinates(latitude, longitude):
    return _finite_number(latitude) and _finite_number(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180


def _frame_time_s(index, fps, pts_ms, previous_time=None):
    """Use monotonic source PTS; fall back when a decoder repeats zero PTS."""
    pts = pts_ms / 1000 if _finite_number(pts_ms) else None
    if pts is not None and pts >= 0 and (previous_time is None or pts > previous_time):
        return pts
    fallback = index / fps
    return fallback if previous_time is None else max(fallback, previous_time + 1 / fps)


class Engine:
    def __init__(self, root, config_store, catalog, store):
        self.root, self.config_store, self.catalog, self.store = root, config_store, catalog, store
        self.lock = threading.RLock()
        self.thread = None
        self.capture_thread = None
        self.stop_event = threading.Event()
        self.jpeg = None
        self.live_pose = None
        self.model_info = {"path": "best.pt", "classes": [], "status": "Load on start"}
        self.state = {"status": "idle", "message": "Choose a recording or camera to begin.", "fps": 0,
                      "frame_index": 0, "video_time_s": 0, "pose": None, "detections": [], "hazards": [],
                      "total_detections": 0, "session_id": None, "frame_width": 720, "frame_height": 1280}
        self.state["capture_pending"] = False

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def active(self):
        return bool((self.thread and self.thread.is_alive()) or (self.capture_thread and self.capture_thread.is_alive()))

    def set_pose(self, pose):
        with self.lock:
            received = time.monotonic()
            sampled = received
            if pose.get("timestamp_ms") is not None:
                timestamp = pose["timestamp_ms"]
                if not _finite_number(timestamp):
                    raise ValueError("timestamp_ms must be a finite Unix timestamp in milliseconds")
                age = time.time() - timestamp / 1000
                if age < -1:
                    raise ValueError("GPS fix timestamp is in the future")
                sampled -= max(0, age)
            self.live_pose = {**pose, "received_monotonic": received, "sampled_monotonic": sampled}

    def resolve_file(self, value):
        path = Path(value)
        path = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_file():
            raise ValueError("Choose an existing file inside the project folder")
        return path

    def start(self, request):
        with self.lock:
            if self.active():
                raise RuntimeError("Stop the current session before starting another")
            config = copy.deepcopy(self.config_store.value)
            model_path = self.resolve_file(config["detection"]["model_path"])
            kind = request.get("source_type", "dataset")
            recording_id = None
            record = None
            if kind == "dataset":
                recording_id = str(request.get("recording_id", "34"))
                record = next((r for r in self.catalog.recordings() if str(r["id"]) == recording_id), None)
                if not record or not record.get("video_path"):
                    raise ValueError("This recording has no video")
                source = str(self.resolve_file(record["video_path"]))
                if record.get("dataset_id") == "comma2k19":
                    published = self.catalog.calibration_for(recording_id)
                    config["calibration"] = effective_calibration(config, published)
                    config["sync_offset_s"] = 0  # Native video frames and fused poses share the published frame clock.
            elif kind == "camera":
                source = int(request.get("source", "0"))
                if not 0 <= source <= 20:
                    raise ValueError("Camera index must be 0–20")
            elif kind == "file":
                source = str(self.resolve_file(request.get("source", "")))
            elif kind == "stream":
                source = str(request.get("source", ""))
                if not source.lower().startswith(("rtsp://", "http://", "https://", "rtmp://")):
                    raise ValueError("Enter an RTSP, HTTP, HTTPS or RTMP camera URL")
            else:
                raise ValueError("Unsupported video source")
            pose_mode = request.get("pose_mode", config["pose"]["mode"])
            if pose_mode not in ("dataset", "manual", "live"):
                raise ValueError("Unsupported pose source")
            if pose_mode == "dataset" and recording_id is None:
                raise ValueError("Dataset GPS requires a dataset recording. Choose manual or live GPS for a camera.")
            if pose_mode == "manual" and any(config["pose"].get(k) is None for k in ("latitude", "longitude", "heading_deg")):
                raise ValueError("Set manual latitude, longitude and heading in camera calibration first")
            session_id = uuid.uuid4().hex[:12]
            self.stop_event.clear()
            self.jpeg = None
            self.live_pose = None  # A prior session's GPS must never geotag a new stream.
            self.state.update(status="running", message="Loading YOLO and opening video…", session_id=session_id,
                              frame_index=0, video_time_s=0, fps=0, total_detections=0, hazards=[], detections=[],
                              pose=None, capture_pending=False, source_type=kind, recording_id=recording_id, pose_mode=pose_mode)
            self.state.update(dataset_id=record.get("dataset_id", "roadsens") if record else None,
                              has_reference_labels=record.get("has_labels", True) if record else None,
                              active_calibration=copy.deepcopy(config["calibration"]),
                              calibration_metadata=self.catalog.calibration_metadata(recording_id) if record and hasattr(self.catalog, "calibration_metadata") else None)
            # Do not persist credentials embedded in a stream URL.
            self.store.start_session(session_id, f"{kind}:{recording_id or ('camera' if kind == 'stream' else source)}", config)
            self.store.session_update(session_id, source_type=kind, recording_id=recording_id,
                                      source_path=source if kind in ("dataset", "file") else None)
            self.thread = threading.Thread(target=self._run, args=(source, kind, recording_id, pose_mode, model_path, config, session_id), daemon=True)
            self.thread.start()
            return self.snapshot()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        with self.lock:
            if self.active():
                self.state["message"] = "Stopping; waiting for the current camera read/inference."
            elif self.state["status"] == "running":
                self.state.update(status="stopped", message="Session stopped.")
        return self.snapshot()

    def _pose(self, mode, recording_id, sensor_time, config, frame_monotonic=None):
        if mode == "dataset":
            pose = self.catalog.pose_at(recording_id, sensor_time, max_gap_s=config["pose"]["max_age_s"])
            if pose:
                pose = {**pose, "source": pose.get("source", "dataset interpolated GPS")}
        elif mode == "manual":
            pose = {**config["pose"], "source": "manual fixed vehicle position", "time_s": sensor_time}
        else:
            with self.lock:
                pose = copy.deepcopy(self.live_pose)
            if pose is None:
                return None
            now = time.monotonic()
            received = pose.pop("received_monotonic")
            sampled = pose.pop("sampled_monotonic", received)
            age = now - sampled
            frame_at = now if frame_monotonic is None else frame_monotonic
            if age < 0 or age > config["pose"]["max_age_s"] or abs(frame_at - sampled) > config["pose"]["max_age_s"]:
                return None
            pose.update(source="live GPS", time_s=sensor_time, fix_age_s=age,
                        fix_time_basis="producer timestamp" if pose.get("timestamp_ms") is not None else "server receipt")
        if not pose or not _valid_coordinates(pose.get("latitude"), pose.get("longitude")) or not _finite_number(pose.get("heading_deg")):
            return None
        pose["heading_deg"] %= 360
        return pose

    def _run(self, source, kind, recording_id, pose_mode, model_path, config, session_id):
        cap = None
        capture_thread = None
        final_status = "completed"
        try:
            from ultralytics import YOLO
            model = YOLO(str(model_path))
            with self.lock:
                self.model_info = {"path": config["detection"]["model_path"], "classes": list(model.names.values()), "status": "Loaded"}
                self.state["model"] = self.model_info
            # Camera and network sources use a latest-frame producer to avoid growing capture latency.
            cap = cv2.VideoCapture()
            if kind == "stream":
                cap.open(source, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000])
            else:
                cap.open(source)
            if not cap.isOpened():
                raise ValueError("Could not open video. Check camera index, stream address or file.")
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            native_fps = cap.get(cv2.CAP_PROP_FPS)
            native_fps = native_fps if math.isfinite(native_fps) and native_fps > 0 else 30
            if kind in ("dataset", "file"):
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                self.store.session_update(session_id, fps=native_fps,
                                          frame_count=int(frame_count) if math.isfinite(frame_count) and frame_count > 0 else None)
            calibration = calibration_from_dict(config["calibration"])
            settings = config["detection"]
            refs = self.catalog.reference_events(recording_id) if recording_id else []
            refs += self.store.references()
            accumulator = HazardAccumulator(session_id, self.store)
            index, processed = -1, 0
            start = time.monotonic()
            latest, capture_lock, capture_done = {}, threading.Lock(), threading.Event()
            capture_thread = None
            if kind in ("camera", "stream"):
                def capture_latest():
                    seq = 0
                    try:
                        while not self.stop_event.is_set():
                            ok, frame = cap.read()
                            if not ok:
                                break
                            with capture_lock:
                                captured_at = time.monotonic()
                                latest.update(frame=frame, seq=seq, at=captured_at - start, captured_monotonic=captured_at)
                            seq += 1
                    finally:
                        # This thread alone accesses/releases live capture. Calling
                        # release concurrently with native read() can crash a backend.
                        try:
                            cap.release()
                        finally:
                            capture_done.set()
                            with self.lock:
                                if self.state.get("session_id") == session_id and self.state.get("capture_pending"):
                                    self.state["capture_pending"] = False
                                    if self.state["status"] == "stopped":
                                        self.state["message"] = "Session stopped."
                capture_thread = threading.Thread(target=capture_latest, daemon=True)
                self.capture_thread = capture_thread
                capture_thread.start()
            previous_seq = -1
            previous_video_time = None
            while not self.stop_event.is_set():
                if capture_thread:
                    with capture_lock:
                        packet = latest.copy()
                    if not packet or packet["seq"] == previous_seq:
                        if capture_done.is_set():
                            raise ValueError("Camera stream ended or disconnected")
                        self.stop_event.wait(0.01)
                        continue
                    previous_seq = packet["seq"]
                    frame, index, video_time = packet["frame"], packet["seq"], packet["at"]
                    frame_monotonic = packet["captured_monotonic"]
                else:
                    # Advance compressed frames without materializing a full 4K
                    # BGR array for frames that the configured stride discards.
                    if not cap.grab():
                        break
                    index += 1
                    if index % settings["frame_stride"]:
                        continue
                    ok, frame = cap.retrieve()
                    if not ok:
                        raise ValueError("Could not decode the selected video frame")
                    native_time = self.catalog.frame_time_s(recording_id, index) if recording_id and hasattr(self.catalog, "frame_time_s") else None
                    video_time = native_time if native_time is not None else _frame_time_s(index, native_fps, cap.get(cv2.CAP_PROP_POS_MSEC), previous_video_time)
                    previous_video_time = video_time
                    frame_monotonic = None
                    # Keep recordings at their native timeline; slow inference is reported as measured FPS.
                    if self.stop_event.wait(max(0, video_time - (time.monotonic() - start))):
                        break
                sensor_time = video_time + config["sync_offset_s"]
                source_height, source_width = frame.shape[:2]
                frame = _resize_for_processing(frame)
                pose = self._pose(pose_mode, recording_id, sensor_time, config, frame_monotonic)
                inference_start = time.monotonic()
                kwargs = dict(persist=True, tracker="bytetrack.yaml", conf=settings["confidence"], imgsz=settings["imgsz"], verbose=False)
                if settings["device"] != "auto":
                    kwargs["device"] = settings["device"]
                result = model.track(frame, **kwargs)[0]
                h, w = frame.shape[:2]
                detections = []
                if result.boxes is not None:
                    boxes = result.boxes
                    ids = boxes.id.cpu().tolist() if boxes.id is not None else [None] * len(boxes)
                    for row, cls, conf, track in zip(boxes.xyxy.cpu().tolist(), boxes.cls.cpu().tolist(), boxes.conf.cpu().tolist(), ids):
                        if len(row) != 4 or not all(_finite_number(value) for value in [*row, cls, conf]) or (track is not None and not _finite_number(track)):
                            continue
                        x1, y1, x2, y2 = row
                        u = min(w - 1, max(0, (x1 + x2) / 2))
                        v = min(h - 1, max(0, y2 if calibration.point_mode == "bottom_center" else (y1 + y2) / 2))
                        is_comma = str(recording_id).startswith("comma2k19-")
                        if is_comma:
                            projection = project_comma_pixel(u, v, w, h, calibration, pose or {})
                            location = projection
                        else:
                            projection = project_pixel(u, v, w, h, calibration)
                            location = locate_hazard(projection, pose, calibration) if pose else {"valid": False, "reason": "No synchronized GPS/heading"}
                        det = {"id": int(track) if track is not None else None, "track_id": int(track) if track is not None else None,
                               "class_name": str(model.names[int(cls)]), "confidence": round(float(conf), 4), "bbox": row,
                               "frame_index": index, "video_time_s": video_time,
                               "frame_width": w, "frame_height": h, "source_type": kind,
                               "distance_m": projection.get("distance_m"), "forward_m": projection.get("forward_m"),
                               "right_m": projection.get("right_m"), "latitude": location.get("latitude"),
                               "longitude": location.get("longitude"), "provisional": not calibration.verified,
                               "projection_reason": projection.get("reason") if not projection.get("valid") else location.get("reason"),
                               "gps_accuracy_m": pose.get("accuracy_m") if pose else None, "match": None,
                               "dataset_id": "comma2k19" if is_comma else "roadsens" if recording_id else None,
                               "has_reference_labels": False if is_comma else self.state.get("has_reference_labels"),
                               "accuracy_note": projection.get("accuracy_note") if is_comma else None,
                               "method": projection.get("method"),
                               "distance_kind": "horizontal_from_camera" if is_comma else "horizontal_from_gps_antenna"}
                        # ByteTrack unconfirmed detections appear live but do not create duplicate map records.
                        if track is not None:
                            accumulator.update(det, sensor_time, recording_id, refs, settings)
                        detections.append(det)
                        color = (50, 195, 240) if not calibration.verified else (178, 220, 30)
                        cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
                        distance_label = f"{det['distance_m']:.1f} m" if det["distance_m"] is not None else "range unavailable"
                        label = f"{det['class_name']} #{det['id'] or '?'} {conf:.0%} | {distance_label}"
                        if det["provisional"]:
                            label += " (provisional)"
                        cv2.putText(frame, label, (max(0, int(x1)), max(18, int(y1) - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.52, color, 2, cv2.LINE_AA)
                encoded, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if encoded:
                    accumulator.save_frame(detections, jpeg.tobytes(), index, video_time, w, h)
                processed += 1
                with self.lock:
                    if encoded:
                        self.jpeg = jpeg.tobytes()
                    pose_message = "fixed manual position" if pose_mode == "manual" and pose else "fused camera pose synchronized" if pose and pose.get("position_reference") == "camera" else "GPS synchronized" if pose else "waiting for valid synchronized GPS and heading"
                    self.state.update(message="Detecting · " + pose_message,
                                      frame_index=index, video_time_s=round(video_time, 3), sensor_time_s=round(sensor_time, 3),
                                      fps=round(processed / max(0.001, time.monotonic() - start), 1),
                                      inference_ms=round((time.monotonic() - inference_start) * 1000),
                                      pose=pose, detections=detections, hazards=list(accumulator.items.values())[-1000:],
                                      total_detections=self.state["total_detections"] + len(detections),
                                      frame_width=w, frame_height=h,
                                      source_frame_width=source_width, source_frame_height=source_height)
            if self.stop_event.is_set():
                final_status = "stopped"
        except Exception as exc:
            final_status = "error"
            with self.lock:
                self.state["message"] = f"{type(exc).__name__}: {exc}"
        finally:
            self.stop_event.set()
            if capture_thread:
                # active() includes a producer that outlives this bounded join,
                # preventing another session from racing a blocked camera read.
                capture_thread.join(timeout=2.5)
            elif cap:
                cap.release()
            with self.lock:
                self.state["status"] = final_status
                self.state["capture_pending"] = bool(capture_thread and not capture_done.is_set())
                if final_status != "error":
                    self.state["message"] = "Recording complete." if final_status == "completed" else "Session stopped."
                if self.state["capture_pending"]:
                    self.state["message"] += " Waiting for the camera backend to release."
            self.store.finish_session(session_id, final_status)
