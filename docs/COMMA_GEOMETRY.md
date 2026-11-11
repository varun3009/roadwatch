# comma2k19 camera and pose geometry

The adapter uses published camera intrinsics and synchronized camera poses. It
still needs a measured camera height and validation against known road distances
before distance and hazard coordinates can be considered calibrated.

## Published camera profile

The official [intrinsics file](https://github.com/commaai/comma2k19/blob/master/notebooks/camera_intrinsics.txt)
provides `fx=fy=910`, `cx=582`, and `cy=437` pixels. The official
[camera implementation](https://github.com/commaai/comma2k19/blob/master/utils/camera.py)
defines the full image size as **1164 × 874** pixels. The resulting matrix is:

```text
910   0 582
  0 910 437
  0   0   1
```

Neither source establishes distortion coefficients or camera height above the
road. The supplied profile therefore assumes zero distortion and starts with an
editable **1.4 m assumed height**. It is saved with `verified=False`. This height
is an application starting value, not a comma2k19 measurement. Image coordinates
can be scaled for a resize preserving aspect ratio; crops, rotations and
letterboxing require corrected intrinsics.

## Orientation discrepancy in the official sources

The [README pose section](https://github.com/commaai/comma2k19#pose-format)
describes `frame_positions` as camera ECEF positions and stores orientations as
Hamilton quaternions with camera axes `[forward, right, down]`. Its stated
rotation direction conflicts with the supplied code: `device_from_ecef()` in
[`utils/camera.py`](https://github.com/commaai/comma2k19/blob/master/utils/camera.py#L81-L90)
constructs `ecef_from_device_rot = rotations_from_quats(orientation_ecef)` and
transposes that matrix when converting ECEF points into the camera frame.
[`utils/orientation.py`](https://github.com/commaai/comma2k19/blob/master/utils/orientation.py#L49-L70)
uses the standard scalar-first Hamilton matrix.

RoadWatch follows the example implementation: **`R(q)` maps camera FRD into
ECEF**, and `R(q).T` maps ECEF into camera FRD. Applying the README direction
literally would transpose the actual camera rotation and give wrong distances.

An independent check uses the first pose and velocity in the official
[`Example_1` segment 40](https://github.com/commaai/comma2k19/tree/master/Example_1):

```text
quaternion wxyz = [0.21243875, -0.80302968, -0.43522125, -0.34726874]
ECEF velocity  = [2.90472390, 4.01603002, 6.20555644] m/s
dot(R(q)[:,0], normalized velocity) = 0.99810185
dot(R(q)[0,:], normalized velocity) = 0.85834983
```

The first expression uses the same forward axis as the example code and closely
aligns with motion. This source discrepancy is documented rather than concealed
by an automatically selected convention. Analytic north/east, pitch and roll
tests also fix the intended direction independently of that sample.

## Projection and position reference

`ecef_pose_to_geodetic(position, orientation, velocity=None)` converts WGS84 ECEF
camera position into latitude, longitude and altitude. It derives true-north
heading from the **horizontal camera optical axis**, preserving the original
ECEF arrays and noting that the reference is the camera. Optional speed is the
3D ECEF velocity magnitude; fix accuracy remains unknown.

`project_comma_pixel(u, v, frame_width, frame_height, calibration, pose)`:

1. Scales the intrinsic matrix and undistorts the image pixel.
2. Converts pinhole `[right, down, forward]` into pose `[forward, right, down]`.
3. Applies the frame's full quaternion to obtain an ECEF ray, then a local ENU ray.
4. Intersects the ray with the horizontal road plane `height_m` below the camera.
5. Converts the intersection back to WGS84 latitude and longitude.

Manual camera pitch, roll, yaw and antenna lever offsets are ignored for this
path: the synchronized quaternion includes camera attitude and `frame_positions`
already refers to the camera. Applying mount angles or an antenna offset again
would apply the same transform twice. Reported forward/right distances use a
horizontal frame aligned with camera heading; distance is horizontal ground
range, not the oblique ray length. The method identifier is
`comma2k19_fused_pose_flat_road`.

Inputs must be finite; quaternions must be within 1% of unit norm and are then
normalized. Their matrices are checked for proper orthonormal rotation. The
projection rejects unsupported aspect ratios, sky and near-horizon rays,
intersections behind camera heading, and distances past the configured limit.
Invalid results expose neither distance nor hazard coordinates.

## Remaining uncertainty

Per-frame camera pose improves attitude handling; it does not measure camera
height or infer the local road's slope. This remains a flat-road approximation.
The detection pixel must represent a point on that road. GNSS/fusion error,
unknown distortion, height error and detection error contribute additional
uncertainty, so no total metre accuracy is invented. Dataset source mode uses a
provisional profile until independently measured and validated. A dataset without
hazard labels supplies camera pose and video, not hazard ground truth.

Run focused checks with:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_comma_geometry.py -q
```
