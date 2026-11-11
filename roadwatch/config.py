"""Validated, persisted settings independent of the web interface."""
from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path

from .geometry import CameraCalibration, calibration_from_dict


def effective_calibration(config, published=None):
    """Keep dataset optics separate; only copy editable road-model assumptions."""
    if published is None:
        return dict(config["calibration"])
    profile = dict(published)
    for key in ("height_m", "max_distance_m", "min_downward_angle_deg", "point_mode"):
        profile[key] = config["calibration"][key]
    profile["verified"] = False  # Published intrinsics alone do not measure road height/distortion.
    return asdict(calibration_from_dict(profile))


def defaults() -> dict:
    return {
        "calibration": asdict(CameraCalibration()),
        "detection": {"model_path": "best.pt", "confidence": 0.25, "imgsz": 640,
                      "device": "cpu", "frame_stride": 3, "match_radius_m": 12,
                      "time_tolerance_s": 5},
        "pose": {"mode": "dataset", "latitude": None, "longitude": None,
                 "heading_deg": None, "accuracy_m": 5, "max_age_s": 3},
        "sync_offset_s": 0,
    }


def number(value, name, low, high):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def validate_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Settings must be an object")
    cfg = defaults()
    if set(raw) - set(cfg):
        raise ValueError("Unknown settings section")
    for section in ("calibration", "detection", "pose"):
        values = raw.get(section, {})
        if not isinstance(values, dict) or set(values) - set(cfg[section]):
            raise ValueError(f"Unknown {section} settings")
        cfg[section].update(values)
    cfg["calibration"] = asdict(calibration_from_dict(cfg["calibration"]))
    d = cfg["detection"]
    d["confidence"] = number(d["confidence"], "Confidence", 0.01, 1)
    for key, low, high in (("imgsz", 160, 2048), ("frame_stride", 1, 60)):
        n = number(d[key], key, low, high)
        if n != int(n):
            raise ValueError(f"{key} must be an integer")
        d[key] = int(n)
    d["match_radius_m"] = number(d["match_radius_m"], "Matching radius", 0.1, 200)
    d["time_tolerance_s"] = number(d["time_tolerance_s"], "Time tolerance", 0, 300)
    if not isinstance(d["model_path"], str) or not d["model_path"].strip():
        raise ValueError("Model path is required")
    if not isinstance(d["device"], str) or d["device"] not in ("cpu", "auto", "0", "1", "mps"):
        raise ValueError("Device must be cpu, auto, 0, 1, or mps")
    p = cfg["pose"]
    if p["mode"] not in ("dataset", "manual", "live"):
        raise ValueError("Choose dataset, manual, or live GPS")
    for key, low, high in (("latitude", -90, 90), ("longitude", -180, 180), ("heading_deg", 0, 360)):
        if p[key] is not None:
            p[key] = number(p[key], key, low, high)
    p["accuracy_m"] = number(p["accuracy_m"], "GPS accuracy", 0, 10000)
    p["max_age_s"] = number(p["max_age_s"], "GPS maximum age", 0.1, 60)
    cfg["sync_offset_s"] = number(raw.get("sync_offset_s", 0), "GPS/video time offset", -3600, 3600)
    return cfg


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.value = validate_config(json.loads(path.read_text("utf-8"))) if path.exists() else defaults()

    def save(self, raw):
        value = validate_config(raw)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
        temp.replace(self.path)
        self.value = value
        return value
