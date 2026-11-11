"""Analytical geometry checks, independent sign checks, and calibration guards."""

from dataclasses import replace
import math
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from roadwatch.geometry import (
    CameraCalibration, calibrate_checkerboard, calibration_from_dict,
    haversine_m, locate_hazard, offset_to_gps, project_pixel,
)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.calibration = CameraCalibration(
            width=1000, height=1000, fx=500, fy=500, cx=500, cy=500,
            height_m=2, pitch_deg=0, verified=True,
        )

    def test_known_ground_intersection_and_distance(self):
        # Pixel ray [0.2, 0.2, 1] reaches y=2 at [2, 2, 10].
        p = project_pixel(600, 600, 1000, 1000, self.calibration)
        self.assertTrue(p["valid"], p)
        self.assertAlmostEqual(p["forward_m"], 10)
        self.assertAlmostEqual(p["right_m"], 2)
        self.assertAlmostEqual(p["distance_m"], math.sqrt(104))
        self.assertFalse(p["provisional"])

    def test_downward_pitch_center_pixel(self):
        p = project_pixel(500, 500, 1000, 1000, replace(self.calibration, pitch_deg=20))
        self.assertTrue(p["valid"], p)
        self.assertAlmostEqual(p["forward_m"], 2 / math.tan(math.radians(20)))
        self.assertAlmostEqual(p["right_m"], 0)

    def test_yaw_clockwise_is_vehicle_right(self):
        c = replace(self.calibration, pitch_deg=20, yaw_deg=30)
        p = project_pixel(500, 500, 1000, 1000, c)
        distance = 2 / math.tan(math.radians(20))
        self.assertAlmostEqual(p["right_m"], distance * math.sin(math.radians(30)))
        self.assertAlmostEqual(p["forward_m"], distance * math.cos(math.radians(30)))

    def test_roll_rotates_image_right_toward_ground(self):
        p = project_pixel(600, 500, 1000, 1000, replace(self.calibration, roll_deg=90))
        self.assertTrue(p["valid"], p)
        self.assertAlmostEqual(p["forward_m"], 10)
        self.assertAlmostEqual(p["right_m"], 0, places=10)

    def test_camera_antenna_offsets_applied_once(self):
        c = replace(self.calibration, offset_forward_m=1.5, offset_right_m=-0.5)
        p = project_pixel(600, 600, 1000, 1000, c)
        self.assertAlmostEqual(p["forward_m"], 11.5)
        self.assertAlmostEqual(p["right_m"], 1.5)

    def test_resize_same_aspect_preserves_geometry(self):
        original = project_pixel(600, 600, 1000, 1000, self.calibration)
        small = project_pixel(300, 300, 500, 500, self.calibration)
        self.assertAlmostEqual(original["distance_m"], small["distance_m"])
        portrait = CameraCalibration()
        self.assertFalse(project_pixel(600, 300, 1280, 720, portrait)["valid"])

    def test_distortion_is_undone_before_projection(self):
        c = replace(self.calibration, distortion=[0.2, -0.02, 0.001, 0.002, 0.0])
        pixels, _ = cv2.projectPoints(
            np.array([[2.0, 2.0, 10.0]]), np.zeros(3), np.zeros(3),
            c.camera_matrix, np.array(c.distortion),
        )
        u, v = pixels.reshape(2)
        p = project_pixel(u, v, 1000, 1000, c)
        self.assertTrue(p["valid"], p)
        self.assertAlmostEqual(p["forward_m"], 10, places=5)
        self.assertAlmostEqual(p["right_m"], 2, places=5)

    def test_unsafe_rays_return_no_numeric_distance(self):
        cases = [
            (500, 500, self.calibration),  # horizon
            (500, 400, self.calibration),  # sky
            (500, 501, self.calibration),  # unstable near horizon
            (-1, 700, self.calibration),
            (math.nan, 700, self.calibration),
            (500, math.inf, self.calibration),
            (500, 600, replace(self.calibration, max_distance_m=2)),
            (500, 600, replace(self.calibration, yaw_deg=180)),
        ]
        for u, v, c in cases:
            with self.subTest(u=u, v=v, calibration=c):
                p = project_pixel(u, v, 1000, 1000, c)
                self.assertFalse(p["valid"])
                self.assertIsNone(p["distance_m"])
                self.assertTrue(p["reason"])

    def test_default_profile_is_provisional(self):
        p = project_pixel(360, 800, 720, 1280, CameraCalibration())
        self.assertTrue(p["valid"])
        self.assertTrue(p["provisional"])

    def test_profile_rejects_invalid_values(self):
        for fields in (
            {"fx": math.nan}, {"pitch_deg": math.inf}, {"width": 100.5},
            {"height_m": 0}, {"fx": 0}, {"cx": 720}, {"distortion": [0, 0]},
            {"distortion": [0, 0, 0, 0, math.nan]}, {"verified": "false"},
            {"point_mode": "unknown"}, {"max_distance_m": -1},
            {"offset_forward_m": math.inf}, {"pitch_degrees": 20},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                calibration_from_dict(fields)


class GpsTests(unittest.TestCase):
    def test_true_north_and_east_heading_signs(self):
        c = CameraCalibration(verified=True)
        p = {"valid": True, "forward_m": 10, "right_m": 3, "provisional": False}
        north = locate_hazard(p, {"latitude": 0, "longitude": 0, "heading_deg": 0, "accuracy_m": 2}, c)
        east = locate_hazard(p, {"latitude": 0, "longitude": 0, "heading_deg": 90}, c)
        self.assertTrue(north["valid"])
        self.assertEqual(north["east_m"], 3)
        self.assertEqual(north["north_m"], 10)
        self.assertAlmostEqual(east["east_m"], 10)
        self.assertAlmostEqual(east["north_m"], -3)
        self.assertGreater(east["longitude"], 0)
        self.assertLess(east["latitude"], 0)
        self.assertEqual(north["accuracy_m"], 2)
        self.assertIsNone(east["accuracy_m"])

    def test_spherical_displacement_distance_and_dateline(self):
        lat, lon = offset_to_gps(43.0, -79.0, 30, 40)
        self.assertAlmostEqual(haversine_m(43.0, -79.0, lat, lon), 50, places=6)
        lat, lon = offset_to_gps(0, 179.99999, 10, 0)
        self.assertLess(lon, 0)
        self.assertAlmostEqual(haversine_m(0, 179.99999, lat, lon), 10, places=6)
        self.assertEqual(offset_to_gps(43, -79, 0, 0), (43, -79))

    def test_missing_bad_pose_or_projection_cannot_create_map_coordinate(self):
        c = CameraCalibration()
        projection = {"valid": True, "forward_m": 10, "right_m": 0}
        for pose in (None, {}, {"latitude": 0, "longitude": 0},
                     {"latitude": math.nan, "longitude": 0, "heading_deg": 0},
                     {"latitude": 91, "longitude": 0, "heading_deg": 0},
                     {"latitude": 0, "longitude": 0, "heading_deg": 0, "accuracy_m": -1}):
            p = locate_hazard(projection, pose, c)
            self.assertFalse(p["valid"], p)
            self.assertIsNone(p["latitude"])
        self.assertFalse(locate_hazard({"valid": False}, {}, c)["valid"])


class CheckerboardTests(unittest.TestCase):
    def test_missing_boards_and_mixed_sizes_rejected(self):
        images = [np.zeros((480, 640), np.uint8)] * 5
        with self.assertRaisesRegex(ValueError, "at least 5"):
            calibrate_checkerboard(images, 7, 5, 0.025)
        with self.assertRaisesRegex(ValueError, "mixed resolutions"):
            calibrate_checkerboard(images + [np.zeros((640, 480), np.uint8)], 7, 5, 0.025)

    def test_duplicate_checkerboard_views_are_not_five_poses(self):
        corners = np.mgrid[0:7, 0:5].T.reshape(-1, 1, 2).astype(np.float32) * 30 + 100
        with patch("roadwatch.geometry.cv2.findChessboardCornersSB", return_value=(True, corners)):
            with self.assertRaisesRegex(ValueError, "Only 1 distinct"):
                calibrate_checkerboard([np.zeros((480, 640), np.uint8)] * 5, 7, 5, 0.025)

    def test_recovers_intrinsics_from_known_varied_board_observations(self):
        k = np.array([[700., 0, 320], [0, 710., 240], [0, 0, 1]])
        board = np.zeros((7 * 5, 3), np.float32)
        board[:, :2] = np.mgrid[0:7, 0:5].T.reshape(-1, 2) * 0.025
        observations = []
        for index in range(8):
            rv = np.array([0.06 * index - 0.2, -0.1 + index * 0.04, 0.025 * index])
            tv = np.array([-0.08 + index * 0.008, -0.06 + index * 0.006, 0.55 + index * 0.035])
            points, _ = cv2.projectPoints(board, rv, tv, k, np.zeros(5))
            observations.append((True, points.astype(np.float32)))
        with patch("roadwatch.geometry.cv2.findChessboardCornersSB", side_effect=observations):
            result = calibrate_checkerboard([np.zeros((480, 640), np.uint8)] * 8, 7, 5, 0.025)
        self.assertGreaterEqual(result["images_used"], 5)
        self.assertAlmostEqual(result["fx"], 700, delta=0.2)
        self.assertAlmostEqual(result["fy"], 710, delta=0.2)
        self.assertLess(result["rms_px"], 0.01)
        self.assertFalse(result["mounting_calibrated"])
        self.assertFalse(result["verified"])


if __name__ == "__main__":
    unittest.main()
