# comma2k19 in RoadWatch

RoadWatch accepts comma2k19 alongside the existing RoadSens recordings. The
official example segment has been downloaded and verified. The full release
downloads separately in the background; its presence is determined by completed
segments and the download status, not by the existence of an archive filename.

comma2k19 is an **unlabeled inference source** here. Existing YOLO weights can
predict potholes, cracks and other supported classes, but these predictions are
not ground truth and cannot establish detection accuracy. RoadSens annotation
files are never attached to comma2k19 recordings.

## Download and discover recordings

From the project directory, resume/download the full dataset with:

```powershell
.\.venv\Scripts\python.exe scripts\download_comma2k19.py --scope all --workers 3
```

The [official project](https://github.com/commaai/comma2k19) publishes an example
segment and links the complete dataset. The full release is approximately
100 GB, split into chunks. Keep enough additional disk space for extraction.
The downloader writes temporary files before publishing completed files. A
partially downloaded video or a segment without synchronized telemetry is not
listed as playable. Existing downloads can be resumed.

The adapter searches below `datasets/comma2k19/`. These layouts are supported:

```text
datasets/comma2k19/
  Example_1/<route>/<segment>/
  Dataset_chunk_1/<route>/<segment>/
  Chunk_1/<route>/<segment>/
```

Each segment contains `video.hevc` and `global_pose/`; the documented
`global_pos/` spelling is accepted too. A frame-preserving `video.mp4` is also
accepted and preferred when present. Native NumPy arrays can have no suffix,
as in the release, or use `.npy`. Required pose arrays are `frame_times`,
`frame_positions` and `frame_orientations`; `frame_velocities` is optional.
Route names replace `|` with `_` on Windows, matching the project's
[filesystem workaround](https://github.com/commaai/comma2k19#workaround-for-microsoft-based-filesystems).

Recording IDs use the normalized route and segment, independently of the chunk
directory. The example and its full-release copy therefore represent one
recording. A valid full-release copy is preferred, preserving saved recording
IDs when additional chunks arrive. Call `catalog.refresh()` after publishing
new segments; `catalog.invalidate()` clears cached metadata without scanning.

## Camera calibration and synchronized GPS

The supplied intrinsic profile uses the published camera matrix at **1164 × 874**:
`fx=fy=910`, `cx=582`, `cy=437`. See the official
[intrinsics](https://github.com/commaai/comma2k19/blob/master/notebooks/camera_intrinsics.txt)
and [camera implementation](https://github.com/commaai/comma2k19/blob/master/utils/camera.py).
The editable initial height of **1.4 m** and zero distortion coefficients are
application assumptions. The profile remains unverified. See
[COMMA_GEOMETRY.md](COMMA_GEOMETRY.md) for projection conventions and the source
orientation discrepancy.

`frame_time_s(recording_id, frame_index)` reads the exact video-frame timestamp
relative to the first frame. Replay uses these timestamps instead of assuming
that every source frame is exactly 50 ms apart. `pose_at()` interpolates camera
positions in ECEF and camera attitude with shortest-arc quaternion interpolation.
It does not extrapolate beyond the video or bridge telemetry gaps over the
configured limit. Ground points are projected using the full synchronized camera
attitude. Manual mounting rotations do not replace that attitude.

If fused position/attitude arrays are missing or corrupt, synchronized
`processed_log/GNSS/live_gnss_ublox/{t,value}` or the equivalent QCOM arrays can
provide the vehicle track. These logs share the device boot clock with
`frame_times`. This fallback has no camera attitude, so full camera-pose hazard
localization is unavailable. The adapter explicitly reports
`pose_mode='gnss_fallback'` instead of inventing an orientation.

Map tracks contain at most 1,200 points per recording. Array mappings are cached
for at most 32 segments. Pose accuracy is reported as unknown: proximity to a
native GPS sample does not measure total hazard-coordinate uncertainty. Height,
road slope, distortion, GNSS/fusion and detection errors still affect distance.

## Validation

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_comma_dataset.py tests/test_comma_geometry.py -q
```

The adapter checks official/chunk layouts, unlabeled reference isolation, invalid
downloads, bounded caches/map responses, stable duplicate handling, real frame
times, ECEF/quaternion interpolation, GNSS fallback and calibration metadata.
The downloaded official sample has 1,200 poses spanning 59.94916 s. Its first
fused position is approximately `37.721000009, -122.472299089`; the nearest native
u-blox GPS observation is 0.1075 s later and 0.604 m away. This cross-check
validates clock/coordinate interpretation, not hazard-distance accuracy.

Dataset publication: [A Commute in Data: The comma2k19 Dataset](https://arxiv.org/abs/1812.05752).
