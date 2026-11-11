"""Published comma2k19 camera intrinsics and camera-pose road projection.

The official README reverses the orientation direction relative to the example
implementation. This module follows ``utils/camera.py`` and the shipped sample:
the Hamilton [w, x, y, z] rotation maps camera [forward, right, down] into ECEF.
See docs/COMMA_GEOMETRY.md for the source and an independent velocity check.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

import cv2
import numpy as np

from .geometry import CameraCalibration, _integer, _number


COMMA_CAMERA_INTRINSICS = {
    "width": 1164, "height": 874,
    "fx": 910.0, "fy": 910.0, "cx": 582.0, "cy": 437.0,
}
COMMA_CAMERA_PROVENANCE = {
    "intrinsics_source": "https://github.com/commaai/comma2k19/blob/master/notebooks/camera_intrinsics.txt",
    "image_size_source": "https://github.com/commaai/comma2k19/blob/master/utils/camera.py",
    "pose_source": "https://github.com/commaai/comma2k19/blob/master/utils/camera.py#L81-L90",
    "orientation_convention": "Hamilton wxyz, camera FRD to ECEF; follows source code and sample",
    "distortion_status": "Not published in these sources; zero coefficients are an assumption",
    "height_status": "Camera height above road is not established by the published intrinsics",
}
COMMA_PROJECTION_METHOD = "comma2k19_fused_pose_flat_road"
COMMA_POSE_SOURCE = "comma2k19 fused camera pose"
_WGS84_A = 6_378_137.0
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)


def comma_camera_calibration(height_m: float = 1.4, **overrides: Any) -> CameraCalibration:
    """Build the published intrinsic profile with an explicitly provisional height.

    ``height_m`` defaults to an editable assumption, never a dataset measurement.
    Frame orientation supplies mounting angles and position is already the camera
    centre, so those manual profile fields are ignored by ``project_comma_pixel``.
    """
    return CameraCalibration(**{
        **COMMA_CAMERA_INTRINSICS, "height_m": height_m,
        "distortion": [0.0] * 5, "pitch_deg": 0.0, "roll_deg": 0.0,
        "yaw_deg": 0.0, "offset_forward_m": 0.0, "offset_right_m": 0.0,
        "verified": False, **overrides,
    })


def _vector(value: Any, length: int, name: str) -> np.ndarray:
    # Reject strings/bools rather than silently interpreting them as coordinates.
    raw = np.asarray(value)
    if raw.shape != (length,) or raw.dtype.kind not in "iuf":
        raise ValueError(f"{name} must contain {length} finite numbers")
    vector = raw.astype(np.float64)
    if not np.isfinite(vector).all():
        raise ValueError(f"{name} must contain {length} finite numbers")
    return vector


def _unit_quaternion(value: Any) -> np.ndarray:
    quaternion = _vector(value, 4, "orientation")
    norm = float(np.linalg.norm(quaternion))
    if not math.isfinite(norm) or not 0.99 <= norm <= 1.01:
        raise ValueError("orientation must be a near-unit Hamilton wxyz quaternion")
    # Stored samples may have roundoff; normalization preserves orthonormality.
    return quaternion / norm


def quaternion_slerp(left: Any, right: Any, fraction: float) -> list[float]:
    """Shortest-arc interpolation for Hamilton wxyz camera orientations."""
    fraction = _number(fraction, "fraction")
    if not 0 <= fraction <= 1:
        raise ValueError("fraction must lie from 0 to 1")
    first, second = _unit_quaternion(left), _unit_quaternion(right)
    dot = float(np.dot(first, second))
    if dot < 0:
        second, dot = -second, -dot
    dot = min(1.0, max(-1.0, dot))
    if dot > 0.9995:
        result = first + fraction * (second - first)
    else:
        angle = math.acos(dot)
        result = (math.sin((1 - fraction) * angle) * first
                  + math.sin(fraction * angle) * second) / math.sin(angle)
    return (result / np.linalg.norm(result)).tolist()


def _ecef_from_camera(orientation: Any) -> tuple[np.ndarray, np.ndarray]:
    quaternion = _unit_quaternion(orientation)
    w, x, y, z = quaternion
    matrix = np.array([
        [1 - 2 * (y*y + z*z), 2 * (x*y - w*z), 2 * (x*z + w*y)],
        [2 * (x*y + w*z), 1 - 2 * (x*x + z*z), 2 * (y*z - w*x)],
        [2 * (x*z - w*y), 2 * (y*z + w*x), 1 - 2 * (x*x + y*y)],
    ])
    if (not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-10)
            or not math.isclose(float(np.linalg.det(matrix)), 1.0, abs_tol=1e-10)):
        raise ValueError("orientation does not produce a proper orthonormal rotation")
    return matrix, quaternion


def _geodetic(position: Any) -> tuple[float, float, float, np.ndarray]:
    position = _vector(position, 3, "ECEF position")
    radius = float(np.linalg.norm(position))
    if not 1_000_000 <= radius <= 100_000_000:
        raise ValueError("ECEF position is not a plausible Earth-referenced camera position")
    x, y, z = position
    horizontal = math.hypot(x, y)
    longitude = math.atan2(y, x)
    if horizontal < 1e-8:
        latitude = math.copysign(math.pi / 2, z)
        altitude = abs(z) - _WGS84_A * (1 - _WGS84_F)
    else:
        latitude = math.atan2(z, horizontal * (1 - _WGS84_E2))
        for _ in range(12):
            normal = _WGS84_A / math.sqrt(1 - _WGS84_E2 * math.sin(latitude)**2)
            updated = math.atan2(z + _WGS84_E2 * normal * math.sin(latitude), horizontal)
            if abs(updated - latitude) < 1e-14:
                latitude = updated
                break
            latitude = updated
        normal = _WGS84_A / math.sqrt(1 - _WGS84_E2 * math.sin(latitude)**2)
        # Choose the numerically stable altitude formula near either pole.
        altitude = (horizontal / math.cos(latitude) - normal if abs(math.cos(latitude)) > 0.5
                    else z / math.sin(latitude) - normal * (1 - _WGS84_E2))
    return math.degrees(latitude), math.degrees(longitude), float(altitude), position


def _enu_from_ecef(latitude: float, longitude: float) -> np.ndarray:
    lat, lon = math.radians(latitude), math.radians(longitude)
    slat, clat, slon, clon = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    return np.array([
        [-slon, clon, 0.0],
        [-slat * clon, -slat * slon, clat],
        [clat * clon, clat * slon, slat],
    ])


def ecef_pose_to_geodetic(position: Any, orientation: Any, velocity: Any = None) -> dict[str, Any]:
    """Convert a comma camera pose to WGS84 and optical-axis true-north heading.

    Orientation follows the official example code (camera FRD -> ECEF), including
    its pitch and roll. It is not a vehicle-heading or antenna-position estimate.
    ``accuracy_m`` stays unknown; no accuracy is manufactured from fused poses.
    Invalid pose inputs raise ``ValueError`` so adapters can skip corrupt rows.
    """
    latitude, longitude, altitude, position = _geodetic(position)
    ecef_from_camera, quaternion = _ecef_from_camera(orientation)
    forward_enu = _enu_from_ecef(latitude, longitude) @ ecef_from_camera[:, 0]
    if math.hypot(float(forward_enu[0]), float(forward_enu[1])) < 1e-8:
        raise ValueError("Camera forward axis is vertical; horizontal heading is undefined")
    heading = math.degrees(math.atan2(float(forward_enu[0]), float(forward_enu[1]))) % 360.0
    speed = None
    velocity_vector = None
    if velocity is not None:
        velocity_vector = _vector(velocity, 3, "ECEF velocity")
        speed = math.hypot(*map(float, velocity_vector))
        if not math.isfinite(speed):
            raise ValueError("ECEF velocity magnitude must be finite")
    return {
        "latitude": latitude, "longitude": longitude, "altitude_m": altitude,
        "heading_deg": heading, "accuracy_m": None, "speed_mps": speed,
        "position_reference": "camera", "source": COMMA_POSE_SOURCE,
        "ecef_position": position.tolist(), "ecef_orientation": quaternion.tolist(),
        "ecef_velocity": None if velocity_vector is None else velocity_vector.tolist(),
        "orientation_convention": "camera_frd_to_ecef_hamilton_wxyz",
    }


def project_comma_pixel(
    u: float, v: float, frame_width: int, frame_height: int,
    calibration: CameraCalibration, pose: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Intersect a pixel ray using the synchronized full camera quaternion.

    The road is a local horizontal plane ``height_m`` below the camera. Forward
    and right are horizontal distances in the camera's optical-axis heading basis.
    Manual mount rotations and GPS lever arms are intentionally ignored because
    the published pose already refers to the camera. Valid results contain both
    the projection and hazard GPS; invalid results contain no numeric distances.
    """
    result: dict[str, Any] = {
        "valid": False, "reason": None, "forward_m": None, "right_m": None,
        "distance_m": None, "latitude": None, "longitude": None, "accuracy_m": None,
        "method": COMMA_PROJECTION_METHOD, "source": COMMA_POSE_SOURCE,
        "position_reference": "camera", "ground_model": "flat_road",
        "provisional": not calibration.verified,
        "accuracy_note": "Fused pose accuracy unknown; road slope, height, intrinsics and detection errors are additional",
    }
    try:
        fw = _integer(frame_width, "frame_width", 1, 32768)
        fh = _integer(frame_height, "frame_height", 1, 32768)
        u, v = _number(u, "u"), _number(v, "v")
        if not 0 <= u < fw or not 0 <= v < fh:
            raise ValueError("Pixel lies outside the frame")
        sx, sy = fw / calibration.width, fh / calibration.height
        if abs(sx / sy - 1.0) > 0.005:
            raise ValueError("Frame aspect ratio differs from calibration; recalibrate or undo crop/rotation")
        if not isinstance(pose, Mapping):
            raise ValueError("A synchronized comma2k19 camera pose is required")
        camera_pose = ecef_pose_to_geodetic(pose.get("ecef_position"), pose.get("ecef_orientation"))
        ecef_from_camera, _ = _ecef_from_camera(camera_pose["ecef_orientation"])
    except (TypeError, ValueError, OverflowError) as exc:
        result["reason"] = str(exc)
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
    enu_from_ecef = _enu_from_ecef(camera_pose["latitude"], camera_pose["longitude"])
    # Pixel pinhole axes RDF -> published camera pose axes FRD.
    ray_enu = enu_from_ecef @ ecef_from_camera @ np.array([1.0, *normalized])
    east, north, up = map(float, ray_enu)
    angle = math.degrees(math.atan2(-up, math.hypot(east, north)))
    if up >= 0 or angle < calibration.min_downward_angle_deg:
        result["reason"] = "Ray is above or too close to the road horizon"
        return result
    scale = calibration.height_m / -up
    east, north = east * scale, north * scale
    heading = math.radians(camera_pose["heading_deg"])
    forward = east * math.sin(heading) + north * math.cos(heading)
    right = east * math.cos(heading) - north * math.sin(heading)
    if forward <= 0:
        result["reason"] = "Road intersection lies behind the camera horizontal heading"
        return result
    distance = math.hypot(east, north)
    if distance > calibration.max_distance_m:
        result["reason"] = "Road intersection exceeds the configured distance limit"
        return result
    ground_enu = np.array([east, north, -calibration.height_m])
    ground_ecef = np.asarray(camera_pose["ecef_position"]) + enu_from_ecef.T @ ground_enu
    latitude, longitude, altitude, _ = _geodetic(ground_ecef)
    result.update(
        valid=True, forward_m=float(forward), right_m=float(right), distance_m=distance,
        latitude=latitude, longitude=longitude, altitude_m=altitude,
        east_m=east, north_m=north, heading_deg=camera_pose["heading_deg"],
        downward_angle_deg=angle,
    )
    return result
