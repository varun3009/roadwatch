"""Analytic ECEF/ENU signs and full-pose projection, independent of live data."""

from dataclasses import replace
import math
import unittest

import cv2
import numpy as np

from roadwatch.comma_geometry import (
    COMMA_CAMERA_INTRINSICS, COMMA_CAMERA_PROVENANCE, COMMA_PROJECTION_METHOD,
    comma_camera_calibration, ecef_pose_to_geodetic, project_comma_pixel,
    quaternion_slerp,
)
from roadwatch.geometry import CameraCalibration


def quaternion_product(left, right):
    w, x, y, z = left
    a, b, c, d = right
    return np.array([w*a-x*b-y*c-z*d, w*b+x*a+y*d-z*c,
                     w*c-x*d+y*a+z*b, w*d+x*c-y*b+z*a])


def axis_quaternion(axis, degrees):
    angle = math.radians(degrees) / 2
    return [math.cos(angle), *(math.sin(angle) * np.asarray(axis))]


def geodetic_ecef(latitude, longitude, altitude=0):
    lat, lon = math.radians(latitude), math.radians(longitude)
    a, flattening = 6378137.0, 1 / 298.257223563
    e2 = flattening * (2 - flattening)
    n = a / math.sqrt(1 - e2 * math.sin(lat)**2)
    return np.array([(n+altitude)*math.cos(lat)*math.cos(lon),
                     (n+altitude)*math.cos(lat)*math.sin(lon),
                     (n*(1-e2)+altitude)*math.sin(lat)])


class CommaPoseTests(unittest.TestCase):
    def setUp(self):
        self.position = np.array([6378139.0, 0.0, 0.0])  # Equator, 2 m altitude.
        # At lat=lon=0, north=ECEF Z, east=ECEF Y and down=-ECEF X.
        self.north_quaternion = np.array([math.sqrt(0.5), 0, -math.sqrt(0.5), 0])
        self.east_quaternion = np.array([0.5, -0.5, -0.5, 0.5])

    def test_known_north_and_east_headings_and_native_numpy_inputs(self):
        north = ecef_pose_to_geodetic(self.position, self.north_quaternion, np.array([0, 3, 4]))
        east = ecef_pose_to_geodetic(self.position, self.east_quaternion)
        self.assertAlmostEqual(north["latitude"], 0)
        self.assertAlmostEqual(north["longitude"], 0)
        self.assertAlmostEqual(north["altitude_m"], 2)
        self.assertAlmostEqual(north["heading_deg"], 0)
        self.assertAlmostEqual(east["heading_deg"], 90)
        self.assertEqual(north["speed_mps"], 5)
        self.assertIsNone(north["accuracy_m"])
        self.assertIsNone(east["speed_mps"])
        self.assertEqual(north["position_reference"], "camera")
        self.assertEqual(north["ecef_position"], self.position.tolist())
        self.assertEqual(north["orientation_convention"], "camera_frd_to_ecef_hamilton_wxyz")

    def test_wgs84_conversion_at_ordinary_southern_and_polar_locations(self):
        for latitude, longitude, altitude in [(37.4, -122.1, 123), (-33.9, 151.2, 42),
                                               (89.99999, 67, 8), (0, 179.9, 0)]:
            pose = ecef_pose_to_geodetic(geodetic_ecef(latitude, longitude, altitude), [1, 0, 0, 0])
            self.assertAlmostEqual(pose["latitude"], latitude, places=8)
            self.assertAlmostEqual(pose["longitude"], longitude, places=8)
            self.assertAlmostEqual(pose["altitude_m"], altitude, places=6)

    def test_actual_official_sample_rotation_matches_velocity_direction(self):
        # First arrays in the official Example_1 route/40/global_pose. Values are
        # deliberately fixed to ensure a future README-based transpose regresses.
        pose = ecef_pose_to_geodetic(
            [-2712087.51680896, -4261670.05595519, 3881014.45392170],
            [0.21243875, -0.80302968, -0.43522125, -0.34726874],
            [2.90472390, 4.01603002, 6.20555644],
        )
        self.assertAlmostEqual(pose["latitude"], 37.721000, places=4)
        self.assertAlmostEqual(pose["longitude"], -122.472299, places=4)
        # The sample is travelling approximately north; the transposed rotation
        # falsely heads northeast and gives an excessively steep optical axis.
        self.assertTrue(pose["heading_deg"] < 10 or pose["heading_deg"] > 350, pose)
        speed_heading = math.degrees(math.atan2(
            -math.sin(math.radians(pose["longitude"])) * 2.90472390
            + math.cos(math.radians(pose["longitude"])) * 4.01603002,
            -math.sin(math.radians(pose["latitude"])) * math.cos(math.radians(pose["longitude"])) * 2.90472390
            - math.sin(math.radians(pose["latitude"])) * math.sin(math.radians(pose["longitude"])) * 4.01603002
            + math.cos(math.radians(pose["latitude"])) * 6.20555644,
        )) % 360
        difference = (pose["heading_deg"] - speed_heading + 180) % 360 - 180
        self.assertLess(abs(difference), 4)

    def test_invalid_positions_quaternions_velocity_are_rejected(self):
        for position, orientation, velocity in [
            ([0, 0, 0], self.north_quaternion, None),
            ([math.nan, 0, 0], self.north_quaternion, None),
            (self.position, [0, 0, 0, 0], None),
            (self.position, [2, 0, 0, 0], None),
            (self.position, [1, 0, 0, math.inf], None),
            (self.position, [1, 0, 0], None),
            (self.position, self.north_quaternion, [0, 0, math.nan]),
            (self.position, [True, False, False, False], None),
            (self.position, ["1", "0", "0", "0"], None),
        ]:
            with self.subTest(position=position, orientation=orientation), self.assertRaises(ValueError):
                ecef_pose_to_geodetic(position, orientation, velocity)
        with self.assertRaisesRegex(ValueError, "vertical"):
            ecef_pose_to_geodetic(self.position, [1, 0, 0, 0])

    def test_quaternion_sign_and_roundoff_do_not_change_pose(self):
        pose = ecef_pose_to_geodetic(self.position, self.north_quaternion)
        alternate = ecef_pose_to_geodetic(self.position, -self.north_quaternion * 1.000001)
        self.assertAlmostEqual(pose["heading_deg"], alternate["heading_deg"])
        self.assertAlmostEqual(np.linalg.norm(alternate["ecef_orientation"]), 1)

    def test_slerp_shortest_arc_unit_norm_and_halfway_heading(self):
        middle = quaternion_slerp(self.north_quaternion, self.east_quaternion, 0.5)
        self.assertAlmostEqual(ecef_pose_to_geodetic(self.position, middle)["heading_deg"], 45)
        equivalent = quaternion_slerp(self.north_quaternion, -self.north_quaternion, 0.5)
        self.assertAlmostEqual(abs(np.dot(equivalent, self.north_quaternion)), 1)
        for fraction in [-1, 2, math.nan]:
            with self.assertRaises(ValueError):
                quaternion_slerp(self.north_quaternion, self.east_quaternion, fraction)


class CommaProjectionTests(unittest.TestCase):
    def setUp(self):
        self.calibration = CameraCalibration(
            width=1000, height=1000, fx=500, fy=500, cx=500, cy=500,
            height_m=2, verified=True,
        )
        self.position = [6378139.0, 0.0, 0.0]
        self.north_quaternion = [math.sqrt(0.5), 0, -math.sqrt(0.5), 0]
        self.pose = ecef_pose_to_geodetic(self.position, self.north_quaternion)

    def project(self, u=600, v=600, calibration=None, pose=None, size=(1000, 1000)):
        return project_comma_pixel(u, v, *size, calibration or self.calibration, pose or self.pose)

    def test_known_horizontal_ground_point_and_gps_signs(self):
        result = self.project()
        self.assertTrue(result["valid"], result)
        self.assertAlmostEqual(result["forward_m"], 10)
        self.assertAlmostEqual(result["right_m"], 2)
        self.assertAlmostEqual(result["north_m"], 10)
        self.assertAlmostEqual(result["east_m"], 2)
        self.assertAlmostEqual(result["distance_m"], math.sqrt(104))
        self.assertGreater(result["latitude"], self.pose["latitude"])
        self.assertGreater(result["longitude"], self.pose["longitude"])
        self.assertAlmostEqual(result["altitude_m"], 0, places=4)
        self.assertEqual(result["method"], COMMA_PROJECTION_METHOD)
        self.assertIsNone(result["accuracy_m"])

    def test_east_heading_gives_east_forward_and_south_right(self):
        pose = ecef_pose_to_geodetic(self.position, [0.5, -0.5, -0.5, 0.5])
        result = self.project(pose=pose)
        self.assertAlmostEqual(result["east_m"], 10)
        self.assertAlmostEqual(result["north_m"], -2)
        self.assertGreater(result["longitude"], 0)
        self.assertLess(result["latitude"], 0)

    def test_pose_pitch_controls_distance_without_manual_mount_angles(self):
        down_20 = quaternion_product(self.north_quaternion, axis_quaternion([0, 1, 0], -20))
        pose = ecef_pose_to_geodetic(self.position, down_20)
        result = self.project(500, 500, pose=pose)
        self.assertTrue(result["valid"], result)
        self.assertAlmostEqual(result["forward_m"], 2 / math.tan(math.radians(20)))
        self.assertAlmostEqual(result["right_m"], 0)
        self.assertAlmostEqual(result["downward_angle_deg"], 20)
        edited = self.project(500, 500, pose=pose, calibration=replace(
            self.calibration, pitch_deg=-50, yaw_deg=150, roll_deg=80,
            offset_forward_m=12, offset_right_m=-8,
        ))
        self.assertEqual(result, edited)

    def test_pose_roll_turns_image_right_into_ground_direction(self):
        roll_90 = quaternion_product(self.north_quaternion, axis_quaternion([1, 0, 0], 90))
        result = self.project(600, 500, pose=ecef_pose_to_geodetic(self.position, roll_90))
        self.assertTrue(result["valid"], result)
        self.assertAlmostEqual(result["forward_m"], 10)
        self.assertAlmostEqual(result["right_m"], 0)

    def test_full_pose_rejects_ground_point_behind_camera_heading(self):
        steep_pitch = quaternion_product(self.north_quaternion, axis_quaternion([0, 1, 0], -80))
        result = self.project(500, 999, pose=ecef_pose_to_geodetic(self.position, steep_pitch))
        self.assertFalse(result["valid"], result)
        self.assertIn("behind", result["reason"])
        self.assertIsNone(result["distance_m"])
        self.assertIsNone(result["latitude"])

    def test_same_aspect_resize_and_distortion_correction(self):
        original = self.project()
        resized = self.project(300, 300, size=(500, 500))
        self.assertAlmostEqual(original["distance_m"], resized["distance_m"])
        calibration = replace(self.calibration, distortion=[0.2, -0.02, 0.001, 0.002, 0.0])
        distorted, _ = cv2.projectPoints(
            np.array([[2.0, 2.0, 10.0]]), np.zeros(3), np.zeros(3),
            calibration.camera_matrix, np.array(calibration.distortion),
        )
        result = self.project(*distorted.reshape(2), calibration=calibration)
        self.assertAlmostEqual(result["forward_m"], 10, places=5)
        self.assertAlmostEqual(result["right_m"], 2, places=5)

    def test_invalid_rays_and_pose_never_return_distances_or_gps(self):
        cases = [
            (500, 500, self.calibration, self.pose, (1000, 1000)),
            (500, 400, self.calibration, self.pose, (1000, 1000)),
            (500, 501, self.calibration, self.pose, (1000, 1000)),
            (-1, 600, self.calibration, self.pose, (1000, 1000)),
            (math.nan, 600, self.calibration, self.pose, (1000, 1000)),
            (500, 600, self.calibration, self.pose, (1000, 800)),
            (500, 600, replace(self.calibration, max_distance_m=2), self.pose, (1000, 1000)),
            (500, 600, self.calibration, None, (1000, 1000)),
            (500, 600, self.calibration, {"ecef_position": self.position}, (1000, 1000)),
        ]
        for u, v, calibration, pose, size in cases:
            result = project_comma_pixel(u, v, *size, calibration, pose)
            with self.subTest(result=result):
                self.assertFalse(result["valid"])
                self.assertIsNone(result["distance_m"])
                self.assertIsNone(result["latitude"])
                self.assertIsNone(result["longitude"])

    def test_published_profile_and_uncertainty_are_explicit(self):
        calibration = comma_camera_calibration()
        self.assertEqual(COMMA_CAMERA_INTRINSICS, {
            "width": 1164, "height": 874, "fx": 910.0,
            "fy": 910.0, "cx": 582.0, "cy": 437.0,
        })
        self.assertFalse(calibration.verified)
        self.assertEqual(calibration.distortion, [0.0] * 5)
        self.assertIn("assumption", COMMA_CAMERA_PROVENANCE["distortion_status"])
        self.assertIn("not established", COMMA_CAMERA_PROVENANCE["height_status"])
        result = project_comma_pixel(582, 600, 1164, 874, calibration, self.pose)
        self.assertTrue(result["valid"], result)
        self.assertTrue(result["provisional"])


if __name__ == "__main__":
    unittest.main()
