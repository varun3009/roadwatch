"""Read-only replay of local recordings, including browser-incompatible HEVC."""
from __future__ import annotations

import bisect
import math
from pathlib import Path
import threading

import cv2
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from .engine import _resize_for_processing


VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".hevc", ".h265", ".m4v"}


class ReplayService:
    def __init__(self, root, catalog, store):
        self.root, self.catalog, self.store = Path(root).resolve(), catalog, store
        self.lock = threading.RLock()
        self.cap = None
        self.current = None
        self.index = -1
        self.last_jpeg = None
        self.info_cache = {}

    def close(self):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
            self.cap, self.current, self.last_jpeg = None, None, None
            self.index = -1

    def _record(self, identity):
        record = self.catalog.recording(identity)
        if not record or not record.get("video_path"):
            raise ValueError("Recording video is unavailable")
        return {**record, "path": record["video_path"], "recording_id": str(identity),
                "label": record.get("label") or f"Recording {identity}", "kind": "dataset"}

    def resolve(self, source_id):
        if not isinstance(source_id, str) or len(source_id) > 220:
            raise ValueError("Choose a saved session or dataset recording")
        kind, separator, identity = source_id.partition(":")
        if not separator or not identity:
            raise ValueError("Choose a saved session or dataset recording")
        if kind == "dataset":
            record = self._record(identity)
        elif kind == "session":
            session = self.store.session(identity)
            if not session:
                raise ValueError("Saved session was not found")
            metadata = session.get("metadata") or {}
            original = session.get("source") or ""
            if metadata.get("media_kind") == "photo":
                raise ValueError("Photos can be viewed in the hazard evidence gallery")
            if metadata.get("recording_id") or original.startswith("dataset:"):
                record = self._record(metadata.get("recording_id") or original.split(":", 1)[1])
            else:
                path = metadata.get("source_path") or (original.split(":", 1)[1] if original.startswith("file:") else None)
                if not path:
                    raise ValueError("This source has saved evidence frames but no full local video recording")
                record = {"path": path, "kind": "upload" if metadata.get("source_type") == "upload" else "file",
                          "label": metadata.get("original_filename") or f"Session {identity}"}
            record["session_id"] = identity
        else:
            raise ValueError("Choose a saved session or dataset recording")
        path = Path(record["path"])
        path = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(self.root) or not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            raise ValueError("The original local video is missing or unavailable")
        return {**record, "path": path, "id": source_id, "available": True}

    def sources(self):
        result = []
        for session in self.store.sessions():
            identity = "session:" + session["id"]
            try:
                record = self.resolve(identity)
            except ValueError:
                continue
            result.append({key: record[key] for key in ("id", "label", "kind", "session_id", "available")})
        for record in self.catalog.recordings():
            if record.get("video_path"):
                result.append({"id": "dataset:" + str(record["id"]), "label": record.get("label") or f"Recording {record['id']}",
                               "recording_id": record["id"], "kind": "dataset", "available": True,
                               "duration_s": record.get("duration_s")})
        return result

    def info(self, source_id):
        record = self.resolve(source_id)
        path = record["path"]
        stamp = (str(path), path.stat().st_size, path.stat().st_mtime_ns)
        with self.lock:
            cached = self.info_cache.get(stamp)
            if cached:
                return {**cached, "id": source_id, "label": record["label"]}
            cap = cv2.VideoCapture(str(path))
            try:
                if not cap.isOpened():
                    raise ValueError("The video decoder could not open this recording")
                fps = record.get("fps") or cap.get(cv2.CAP_PROP_FPS)
                fps = float(fps) if isinstance(fps, (int, float)) and math.isfinite(fps) and fps > 0 else 30.0
                count = record.get("frame_count") or cap.get(cv2.CAP_PROP_FRAME_COUNT)
                count = int(count) if math.isfinite(count) and 0 < count < 100_000_000 else 0
                duration = record.get("duration_s") or (count / fps if count else 0)
                if not math.isfinite(duration) or duration <= 0:
                    raise ValueError("Video duration could not be determined")
                times = None
                if str(record.get("recording_id", "")).startswith("comma2k19-"):
                    times = [self.catalog.frame_time_s(record["recording_id"], index) for index in range(count)]
                    if len(times) > 1:
                        duration = times[-1] + (times[-1] - times[-2])
                value = {"duration_s": duration, "fps": fps, "frame_count": count,
                         "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                         "kind": record["kind"], "preview_fps": 5, "audio": False,
                         "note": "Original video preview. Detection evidence and annotations are available in the hazard library.",
                         "_times": times}
                # Bounded metadata cache. It contains no decoded video frames.
                if len(self.info_cache) >= 64:
                    self.info_cache.pop(next(iter(self.info_cache)))
                self.info_cache[stamp] = value
                return {**value, "id": source_id, "label": record["label"]}
            finally:
                cap.release()

    def frame(self, source_id, time_s):
        if not math.isfinite(time_s) or time_s < 0:
            raise ValueError("Replay time must be a finite nonnegative number")
        record = self.resolve(source_id)
        info = self.info(source_id)
        if time_s > info["duration_s"] + 0.1:
            raise ValueError("Replay time is outside the video")
        times = info.get("_times")
        target = max(0, bisect.bisect_right(times, time_s) - 1) if times else int(time_s * info["fps"])
        if info["frame_count"]:
            target = min(target, info["frame_count"] - 1)
        path = str(record["path"])
        with self.lock:
            if self.current != path or target < self.index or self.cap is None:
                self.close()
                self.cap = cv2.VideoCapture(path)
                if not self.cap.isOpened():
                    self.close()
                    raise ValueError("The video decoder could not open this recording")
                self.current = path
            if target == self.index and self.last_jpeg:
                return self.last_jpeg, times[target] if times else target / info["fps"]
            # Raw HEVC does not reliably implement random seek. Reopen for a
            # backward seek and advance with grab(), materializing one frame.
            while self.index < target:
                if not self.cap.grab():
                    raise ValueError("The requested frame could not be decoded")
                self.index += 1
            ok, frame = self.cap.retrieve()
            if not ok:
                raise ValueError("The requested frame could not be decoded")
            ok, encoded = cv2.imencode(".jpg", _resize_for_processing(frame), [cv2.IMWRITE_JPEG_QUALITY, 82])
            if not ok:
                raise ValueError("Could not render the replay frame")
            self.last_jpeg = encoded.tobytes()
            return self.last_jpeg, times[target] if times else target / info["fps"]


def make_replay_router(service):
    router = APIRouter()

    @router.get("/api/replay/sources")
    def sources():
        return {"sources": service.sources()}

    @router.get("/api/replay/info")
    def info(source_id: str):
        try:
            return {key: value for key, value in service.info(source_id).items() if not key.startswith("_")}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/replay/frame")
    def frame(source_id: str, time_s: float = Query(0, ge=0)):
        try:
            jpeg, timestamp = service.frame(source_id, time_s)
            return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Frame-Time": str(timestamp)})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/replay/video")
    def original(source_id: str):
        try:
            record = service.resolve(source_id)
            return FileResponse(record["path"], filename=record["path"].name)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    return router
