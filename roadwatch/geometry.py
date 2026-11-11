"""Camera-to-road projection and geolocation with explicit coordinate conventions.

This is a monocular, locally flat road model. It cannot infer road slope or depth
from an arbitrary object: the selected pixel must lie on the road surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
import math
from typing import Any, Mapping, Sequence

import cv2
import numpy as np


EARTH_RADIUS_M = 6_371_008.8
PROJECTION_METHOD = "calibrated_ray_flat_road"


def _number(value: Any, name: str) -> float:
    if isinstance(value, (bool, str, bytes)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    number = _number(value, name)
    if not number.is_integer() or not minimum <= number <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return int(number)


@dataclass(frozen=True)
class CameraCalibration:
    """Intrinsics at width x height plus camera pose relative to GPS antenna.

    Distortion uses OpenCV's standard pinhole coefficient order. A fisheye model
    must be rectified before using this module. Mount angles are degrees; all
    offsets and heights are metres. ``verified`` is a user acceptance flag, not
    a guarantee of measurement accuracy.
    """

    width: int = 720
    height: int = 1280
    fx: float = 700.0
    fy: float = 700.0
    cx: float = 360.0
    cy: float = 640.0
    distortion: list[float] = field(default_factory=lambda: [0.0] * 5)
    height_m: float = 1.4
    pitch_deg: float = 10.0
    roll_deg: float = 0.0
    yaw_deg: float = 0.0
    offset_forward_m: float = 0.0
    offset_right_m: float = 0.0
    verified: bool = False
    max_distance_m: float = 80.0
    min_downward_angle_deg: float = 2.0
    point_mode: str = "bottom_center"

    def __post_init__(self) -> None:
        for name in ("width", "height"):
            object.__setattr__(self, name, _integer(getattr(self, name), name, 32, 32768))
        for name in (
            "fx", "fy", "cx", "cy", "height_m", "pitch_deg", "roll_deg", "yaw_deg",
            "offset_forward_m", "offset_right_m", "max_distance_m", "min_downward_angle_deg",
        ):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        for name in ("fx", "fy"):
            if not 1.0 <= getattr(self, name) <= 1_000_000.0:
                raise ValueError(f"{name} must be from 1 to 1000000 pixels")
        if not 0 <= self.cx < self.width or not 0 <= self.cy < self.height:
            raise ValueError("Principal point cx/cy must lie inside the calibration image")
        if not 0.05 <= self.height_m <= 20.0:
            raise ValueError("height_m must be from 0.05 to 20 metres")
        if not -89.0 <= self.pitch_deg <= 89.0:
            raise ValueError("pitch_deg must be from -89 to 89 degrees")
        if any(abs(getattr(self, name)) > 180 for name in ("roll_deg", "yaw_deg")):
            raise ValueError("roll_deg and yaw_deg must be from -180 to 180 degrees")
        if any(abs(getattr(self, name)) > 20 for name in ("offset_forward_m", "offset_right_m")):
            raise ValueError("Camera lever offsets must be within 20 metres of the GPS antenna")
        if not 0.1 <= self.max_distance_m <= 1000:
            raise ValueError("max_distance_m must be from 0.1 to 1000 metres")
        if not 0.1 <= self.min_downward_angle_deg <= 45:
            raise ValueError("min_downward_angle_deg must be from 0.1 to 45 degrees")
        if not isinstance(self.verified, bool):
            raise ValueError("verified must be true or false")
        if self.point_mode not in ("bottom_center", "center"):
            raise ValueError("point_mode must be 'bottom_center' or 'center'")
        if not isinstance(self.distortion, (list, tuple, np.ndarray)):
            raise ValueError("distortion must be a list of OpenCV pinhole coefficients")
        try:
            coefficients = [_number(v, "distortion coefficient") for v in self.distortion]
        except (TypeError, ValueError) as exc:
            raise ValueError("distortion must contain finite numeric coefficients") from exc
        if len(coefficients) not in (4, 5, 8, 12, 14):
            raise ValueError("distortion must have 4, 5, 8, 12, or 14 coefficients")
        if any(abs(v) > 100 for v in coefficients):
            raise ValueError("distortion coefficients must be within -100 to 100")
        object.__setattr__(self, "distortion", coefficients)

    @property
    def camera_matrix(self) -> np.ndarray:
        return np.array([[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]])


def calibration_from_dict(data: Mapping[str, Any] | None) -> CameraCalibration:
    """Build and validate a profile, filling omitted fields with defaults."""
    if data is None:
        return CameraCalibration()
    if not isinstance(data, Mapping):
        raise ValueError("Camera calibration must be an object")
    unknown = set(data) - {f.name for f in fields(CameraCalibration)}
    if unknown:
        raise ValueError(f"Unknown calibration fields: {', '.join(sorted(map(str, unknown)))}")
    return CameraCalibration(**dict(data))


def _mount_rotation(calibration: CameraCalibration) -> np.ndarray:
    pitch, roll, yaw = map(math.radians, (calibration.pitch_deg, calibration.roll_deg, calibration.yaw_deg))
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    cy, sy = math.cos(yaw), math.sin(yaw)
    # Coordinates are [right, down, forward], a right-handed frame.
    pitch_rotation = np.array([[1, 0, 0], [0, cp, sp], [0, -sp, cp]])
    roll_rotation = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]])
    yaw_rotation = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    return yaw_rotation @ pitch_rotation @ roll_rotation


def project_pixel(
    u: float, v: float, frame_width: int, frame_height: int,
    calibration: CameraCalibration,
) -> dict[str, Any]:
    """Intersect an undistorted pixel ray with the local ground plane.

    Results are relative to the vehicle GPS antenna. Invalid projections never
    return numerical distances; callers must check ``valid`` before displaying.
    Intrinsics scale only for a resize with the same aspect ratio (0.5% tolerance).
    Cropping, letterboxing, image rotation, and stabilization need their own
    corrected intrinsics and cannot be represented by changing frame dimensions.
    """
    result: dict[str, Any] = {
        "valid": False, "reason": None, "forward_m": None, "right_m": None,
        "distance_m": None, "method": PROJECTION_METHOD,
        "provisional": not calibration.verified, "ground_model": "flat_road",
    }
    try:
        fw = _integer(frame_width, "frame_width", 1, 32768)
        fh = _integer(frame_height, "frame_height", 1, 32768)
        u, v = _number(u, "u"), _number(v, "v")
    except ValueError as exc:
        result["reason"] = str(exc)
        return result
    if not (0 <= u < fw and 0 <= v < fh):
        result["reason"] = "Pixel lies outside the frame"
        return result
    sx, sy = fw / calibration.width, fh / calibration.height
    if abs(sx / sy - 1.0) > 0.005:
        result["reason"] = "Frame aspect ratio differs from calibration; recalibrate or undo crop/rotation"
        return result
    matrix = calibration.camera_matrix.copy()
    matrix[0, :] *= sx
    matrix[1, :] *= sy
    try:
        normalized = cv2.undistortPoints(
            np.array([[[u, v]]], dtype=np.float64), matrix,
            np.asarray(calibration.distortion, dtype=np.float64),
        ).reshape(2)
    except cv2.error:
        result["reason"] = "Lens distortion correction failed"
        return result
    if not np.isfinite(normalized).all():
        result["reason"] = "Lens distortion correction produced an invalid ray"
        return result
    right, down, forward = _mount_rotation(calibration) @ np.array([*normalized, 1.0])
    angle = math.degrees(math.atan2(down, math.hypot(right, forward)))
    if down <= 0 or angle < calibration.min_downward_angle_deg:
        result["reason"] = "Ray is above or too close to the road horizon"
        return result
    scale = calibration.height_m / down
    ground_forward = float(forward * scale + calibration.offset_forward_m)
    ground_right = float(right * scale + calibration.offset_right_m)
    if forward <= 0 or ground_forward <= 0:
        result["reason"] = "Road intersection lies behind the vehicle"
        return result
    distance = math.hypot(ground_forward, ground_right)
    if distance > calibration.max_distance_m:
        result["reason"] = "Road intersection exceeds the configured distance limit"
        return result
    result.update(
        valid=True, forward_m=ground_forward, right_m=ground_right,
        distance_m=distance, downward_angle_deg=angle,
    )
    return result


def _validate_lat_lon(latitude: Any, longitude: Any) -> tuple[float, float]:
    latitude = _number(latitude, "latitude")
    longitude = _number(longitude, "longitude")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("GPS latitude/longitude are out of range")
    return latitude, longitude


def offset_to_gps(latitude: float, longitude: float, east_m: float, north_m: float) -> tuple[float, float]:
    """Apply a local east/north displacement using a spherical geodesic.

    The mean Earth radius model is adequate at road inspection distances; it
    does not improve or certify the source GNSS fix accuracy.
    """
    latitude, longitude = _validate_lat_lon(latitude, longitude)
    east_m, north_m = _number(east_m, "east_m"), _number(north_m, "north_m")
    distance = math.hypot(east_m, north_m)
    if distance == 0:
        return latitude, longitude
    bearing = math.atan2(east_m, north_m)
    angular_distance = distance / EARTH_RADIUS_M
    lat1, lon1 = math.radians(latitude), math.radians(longitude)
    lat2 = math.asin(max(-1.0, min(1.0,
        math.sin(lat1) * math.cos(angular_distance)
        + math.cos(lat1) * math.sin(angular_distance) * math.cos(bearing))))
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(lat1),
        math.cos(angular_distance) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lon2) + 180.0) % 360.0 - 180.0


def haversine_m(latitude1: float, longitude1: float, latitude2: float, longitude2: float) -> float:
    """Great-circle distance in metres, including across the antimeridian."""
    latitude1, longitude1 = _validate_lat_lon(latitude1, longitude1)
    latitude2, longitude2 = _validate_lat_lon(latitude2, longitude2)
    lat1, lat2 = math.radians(latitude1), math.radians(latitude2)
    dlat, dlon = lat2 - lat1, math.radians(longitude2 - longitude1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(max(0.0, a))))


def locate_hazard(
    projection: Mapping[str, Any], pose: Mapping[str, Any] | None,
    calibration: CameraCalibration,
) -> dict[str, Any]:
    """Rotate antenna-relative road offsets by true-north heading and locate.

    Heading is clockwise from true north, independent of camera mounting yaw.
    ``accuracy_m`` is the source GPS fix accuracy, not total hazard uncertainty.
    """
    result: dict[str, Any] = {
        "valid": False, "reason": None, "latitude": None, "longitude": None,
        "accuracy_m": None, "method": PROJECTION_METHOD,
        "provisional": bool(projection.get("provisional", True)) or not calibration.verified,
        "ground_model": "flat_road",
    }
    if not projection.get("valid"):
        result["reason"] = projection.get("reason") or "Invalid road projection"
        return result
    if not isinstance(pose, Mapping):
        result["reason"] = "A synchronized GPS fix and vehicle heading are required"
        return result
    try:
        lat, lon = _validate_lat_lon(pose.get("latitude"), pose.get("longitude"))
        heading = _number(pose.get("heading_deg"), "heading_deg") % 360.0
        forward = _number(projection.get("forward_m"), "forward_m")
        right = _number(projection.get("right_m"), "right_m")
        accuracy = pose.get("accuracy_m")
        if accuracy is not None:
            accuracy = _number(accuracy, "accuracy_m")
            if accuracy < 0:
                raise ValueError("accuracy_m cannot be negative")
    except ValueError as exc:
        result["reason"] = str(exc)
        return result
    heading_radians = math.radians(heading)
    east = forward * math.sin(heading_radians) + right * math.cos(heading_radians)
    north = forward * math.cos(heading_radians) - right * math.sin(heading_radians)
    latitude, longitude = offset_to_gps(lat, lon, east, north)
    result.update(
        valid=True, latitude=latitude, longitude=longitude, east_m=east, north_m=north,
        accuracy_m=accuracy, heading_deg=heading,
        accuracy_note="GPS fix accuracy only; ground slope, calibration and detection error are additional",
    )
    return result


def calibrate_checkerboard(
    images: Sequence[np.ndarray], columns: int, rows: int, square_size_m: float,
) -> dict[str, Any]:
    """Estimate standard pinhole intrinsics from at least five distinct views.

    ``columns``/``rows`` count inner corners, not squares. Provide original,
    unrotated, uncropped images at one resolution and vary board position, tilt,
    and distance. This estimates lens intrinsics only, never mounting geometry.
    Raises ValueError for invalid input or insufficient usable, distinct views.
    """
    columns = _integer(columns, "columns", 3, 50)
    rows = _integer(rows, "rows", 3, 50)
    square_size_m = _number(square_size_m, "square_size_m")
    if not 0.001 <= square_size_m <= 5.0:
        raise ValueError("square_size_m must be from 0.001 to 5 metres")
    if not isinstance(images, (list, tuple)) or len(images) < 5:
        raise ValueError("Provide at least five checkerboard images with different poses")
    image_size: tuple[int, int] | None = None
    gray_images: list[np.ndarray] = []
    # Validate every input before attempting detection, including unusable views.
    for index, image in enumerate(images):
        if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.size == 0:
            raise ValueError(f"Image {index + 1} must be a nonempty uint8 image")
        if image.ndim == 2:
            gray = image
        elif image.ndim == 3 and image.shape[2] in (3, 4):
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY if image.shape[2] == 3 else cv2.COLOR_BGRA2GRAY)
        else:
            raise ValueError(f"Image {index + 1} must be grayscale, BGR, or BGRA")
        size = (gray.shape[1], gray.shape[0])
        if min(size) < 32 or max(size) > 32768:
            raise ValueError("Checkerboard image dimensions must be from 32 to 32768 pixels")
        if image_size is not None and size != image_size:
            raise ValueError("Checkerboard images have mixed resolutions; use one original resolution")
        image_size = size
        gray_images.append(gray)
    assert image_size is not None
    object_template = np.zeros((columns * rows, 3), np.float32)
    object_template[:, :2] = np.mgrid[0:columns, 0:rows].T.reshape(-1, 2) * square_size_m
    image_points: list[np.ndarray] = []
    accepted_indices: list[int] = []
    rejected: list[dict[str, Any]] = []
    minimum_movement = max(2.0, math.hypot(*image_size) * 0.005)
    for index, gray in enumerate(gray_images):
        found, corners = cv2.findChessboardCornersSB(
            gray, (columns, rows), flags=cv2.CALIB_CB_NORMALIZE_IMAGE,
        )
        if not found or corners is None or len(corners) != columns * rows or not np.isfinite(corners).all():
            rejected.append({"index": index, "reason": "checkerboard_not_found"})
            continue
        corners = np.asarray(corners, dtype=np.float32).reshape(-1, 1, 2)
        if any(min(
            float(np.linalg.norm(corners - previous, axis=2).mean()),
            float(np.linalg.norm(corners[::-1] - previous, axis=2).mean()),
        ) < minimum_movement for previous in image_points):
            rejected.append({"index": index, "reason": "duplicate_or_nearly_identical_pose"})
            continue
        image_points.append(corners)
        accepted_indices.append(index)
    if len(image_points) < 5:
        raise ValueError(
            f"Only {len(image_points)} distinct checkerboard poses found; at least 5 are required. "
            "Vary board position, tilt and distance with the full board visible."
        )
    try:
        rms, matrix, distortion, rotations, translations = cv2.calibrateCamera(
            [object_template.copy() for _ in image_points], image_points, image_size, None, None,
        )
    except cv2.error as exc:
        raise ValueError("Checkerboard calibration failed; use sharper images with more varied board poses") from exc
    if not math.isfinite(rms) or not np.isfinite(matrix).all() or not np.isfinite(distortion).all():
        raise ValueError("Calibration returned invalid parameters; capture more varied checkerboard poses")
    intrinsic_fields = {
        "width": image_size[0], "height": image_size[1],
        "fx": float(matrix[0, 0]), "fy": float(matrix[1, 1]),
        "cx": float(matrix[0, 2]), "cy": float(matrix[1, 2]),
        "distortion": distortion.reshape(-1).tolist(), "verified": False,
    }
    calibration_from_dict(intrinsic_fields)
    errors: list[float] = []
    for corners, rotation, translation in zip(image_points, rotations, translations):
        projected, _ = cv2.projectPoints(object_template, rotation, translation, matrix, distortion)
        errors.append(float(np.sqrt(np.mean(np.sum((corners - projected) ** 2, axis=2)))))
    return {
        **intrinsic_fields, "camera_matrix": matrix.tolist(), "rms_px": float(rms),
        "per_view_rms_px": errors, "images_used": len(image_points),
        "images_supplied": len(images), "accepted_indices": accepted_indices, "rejected": rejected,
        "columns": columns, "rows": rows, "square_size_m": square_size_m,
        "mounting_calibrated": False,
        "note": "Intrinsics only. Measure camera height, mounting angles, GPS offsets and validate against known road distances before marking verified.",
    }
