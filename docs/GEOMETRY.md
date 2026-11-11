# Camera calibration and hazard geolocation

`roadwatch.geometry` uses a standard OpenCV pinhole camera with radial/tangential
distortion. The stored calibration width and height must match the original
image orientation. A resize with the same aspect ratio scales the intrinsics;
a crop, rotation, letterbox or image stabilization requires corrected intrinsics.
Portrait frames are never silently swapped to landscape. The standard OpenCV
model does not cover an unrectified fisheye image.

Image `u` points right and `v` points down. The camera's optical axis points
forward. Vehicle offsets are `forward_m` and `right_m`. Positive pitch points
the camera down, positive yaw points it to the vehicle's right, and positive
roll rotates the image-right ray toward the ground. The rotation order is
camera roll, pitch, then vehicle yaw. Mounting angles are degrees. Height and
offsets are metres. `height_m` is the camera height above the road, while
`offset_forward_m` and `offset_right_m` locate the camera relative to the GPS
antenna. A positive forward offset means the camera is ahead of the antenna.

The selected detection pixel is undistorted, rotated into the vehicle frame,
and intersected with a locally flat road plane. `distance_m` is horizontal
distance from the GPS antenna to that intersection; it is not optical range.
Use a pixel on the road: bottom center is useful for a box marking a road
contact, while center may better represent a flat pothole or crack region.
Choose and validate this per detector. Elevated objects, hills, banking,
suspension movement and a box extending beyond the road break this assumption.
Rays above/near the horizon, outside the image, behind the vehicle or beyond the
configured range return `valid: false` with null distances and an explicit reason.

Vehicle heading is clockwise from **true north**. Forward/right offsets rotate
to local east/north and then use a spherical geodesic to compute GPS coordinates.
For heading 0°, forward is north and right is east; for heading 90°, forward is
east and right is south. Camera yaw is already included in the road projection
and must not be added to heading again. GPS fixes and headings must be synchronized
with the source video frame; a location attached to an unrelated image cannot
provide an accurate hazard coordinate. Missing pose data never produces a map pin.
`accuracy_m` is source GNSS accuracy only; it excludes image localization,
calibration, heading, road slope and synchronization error.

Default intrinsics and mounting values are examples. Results remain
`provisional: true` until the operator validates intrinsics, mounting measurements,
GPS offsets and measured road reference distances and sets `verified: true`.
That flag records operator acceptance and does not certify an uncertainty bound.

`calibrate_checkerboard(images, columns, rows, square_size_m)` expects at least
five useful distinct views at one resolution. Columns and rows count **inner
corners**, and square size is in metres. Capture sharp images with the whole
board visible at varied positions, tilts and distances; include edges of the
image. Duplicate or nearly identical detected views are discarded. The returned
RMS and per-view RMS are reprojection errors in pixels. Intrinsic calibration
does not measure camera mounting or GPS offsets, so its output remains unverified.
Copy only width, height, fx, fy, cx, cy and distortion from the checkerboard result
into a camera profile; diagnostic fields are not profile fields.

OpenCV's [camera calibration reference](https://docs.opencv.org/4.6.0/d9/d0c/group__calib3d.html)
documents the pinhole distortion model, `undistortPoints`, checkerboard inner
corner ordering and the reprojection objective used by `calibrateCamera`.
