# Validation

Verified locally against the supplied model and source data. RoadSens uses the default unmeasured camera profile; comma2k19 uses published optics with an assumed mounting height. All estimated hazard coordinates remain provisional.

## Automated checks

- Run `python -m pytest -q` to verify the current checkout.
- The Vue dashboard is served directly from `roadwatch/static/`. Check module syntax with `node --check` and verify navigation, live detection, uploads, calibration and replay in a browser.
- Tests cover known camera/road geometry, heading and mounting signs, checkerboard intrinsics recovery, invalid projections, sparse GPS gaps, workbook mappings, duplicate event windows, import provenance, timestamp freshness, tracking identity and classification changes, persistence and HTTP/WebSocket behavior.
- comma2k19 checks also cover native frame clocks, ECEF/quaternion projection, unlabeled reference isolation, GNSS fallback, source-specific calibration, chunk discovery/deduplication, resumable HTTP transfers, publisher hashes, safe ZIP extraction, CRC failures, concurrent status writes, pause controls and worker error propagation.

## Uploads, replay and saved detections

- Detection replay now runs YOLO as frames are played or sought, draws prominent bounding boxes, logs hazards and saves annotated evidence. A real uploaded sample frame produced a Longitudinal prediction and saved photo. Requesting the identical frame again returned the persisted analysis with unchanged observation count. Unit tests also verify atomic rollback, model/source/settings invalidation, source aliases, restart reuse, backward association, the evidence cap and shared inference ownership.
- An actual five-second uploaded clip was played through the new detection UI: 26 visited frames produced three associated hazard tracks and five saved photos. Seeking/restarting at a cached frame preserved the original eight total observations.

- SQLite migration retains earlier sessions and records. JPEG evidence is stored as BLOBs with SHA-256 hashes and per-frame metadata. A database trigger and reserved writer transaction enforce the ten-frame limit; forty concurrent writers saved exactly ten frames. Live tracking captures frames at least 0.5 seconds apart, up to the cap. Observation counts remain independent of saved-frame counts.
- The full native comma2k19 sample was replayed through the actual model after this change: eight detections yielded four tracked crack predictions, each with one spaced evidence frame. Persisted frame counts matched the live state.
- An actual five-second sample video upload yielded one tracked crack prediction with three evidence images. A still photo from the same historical recording yielded one crack prediction with one image. Another image yielded zero predictions and correctly created no hazard.
- Restarting the server retained hazard images, uploaded media, and completed job status.
- Replay decoder tests used actual generated video frames to verify forward/backward seeking, native comma frame timestamps, the final frame, repeated frame requests, original-file Range downloads, and rejection of missing/external sources. Uploaded GPS tests cover fixed capture proxies, user-selected hazard points, timestamp interpolation, dateline wrapping, missing coverage and gaps.
- Test discovery is restricted to `tests/`, avoiding recursive collection through the full dataset. The completed comma catalog contains 2,035 validated recordings; an isolated initial catalog scan took 12.57 seconds, with subsequent in-process access cached.

## Actual comma2k19 replay

- Downloaded and verified the official example and began the ten-chunk full release through the background worker. Successfully paused with 33.23 GB retained and resumed through the dashboard. Completed segments are usable before the remaining archives finish.
- Replayed all 1,200 sample frames through the actual HTTP API, `best.pt`, ByteTrack and SQLite pipeline, processing every third frame. Eight detections produced four tracked Longitudinal crack predictions, all with provisional GPS markers and no ground-truth matches. CSV export contained the four session hazards.
- The first estimated horizontal camera-to-anchor range was 4.930 m; its marker was approximately `37.721055119, -122.472304989`. Published optics, native timestamps and synchronized camera attitude were used with an assumed 1.4 m height and locally flat road. These are pipeline outputs, not measured accuracy.
- The browser displayed the sample video, California route, hazard markers, published intrinsic profile, unlabeled status and download controls. Native pose accuracy is unknown and is displayed accordingly.
- With 266 complete segments, pruned filesystem discovery took 0.135 s and initial catalog construction 1.45 s during background extraction. Cached roster access took 0.000064 s. These are local measurements, not throughput guarantees.

## Actual model and recording

- Inspected `best.pt`: Longitudinal, Transverse, Alligator and Pothole classes.
- Replayed recording 33 through the actual HTTP session API, YOLO, ByteTrack, projection, SQLite persistence, and browser map. Source video is 2160 × 3840; processing uniformly scales to 720 × 1280.
- At approximately 1.9–2.6 seconds, one ByteTrack identity produced seven observations. The model alternated between pothole and crack classes. Exactly one hazard record and map marker remained, with the class variation disclosed. The strongest observed label in this run was Longitudinal, confidence 0.6448. Earlier Pothole evidence reached 0.5374.
- The estimated last ground range was 2.234 m. The refined marker was at latitude 24.37507610, longitude 88.62205421. Reported GPS accuracy was approximately 10.1 m, and calibration/range error is unmeasured. These are example outputs, not surveyed accuracy measurements.
- Persisted one hazard and verified current-session CSV export contained that same identifier.
- Recording 33's annotations are bumps, so the pothole/crack detection correctly received no reference match. Separate importer/matching tests cover positive CSV/XLSX spatial and temporal candidates.
- The browser displayed the vehicle route, the geolocated hazard, class variation, provisional range, GPS coordinates, reference markers and source controls. Camera configuration saved successfully through the UI.

## Performance check

For 60 native frames from recording 33, stride three selected the same 20 frames. Reading every frame took a mean 5.194 seconds; grabbing every frame and retrieving selected frames took 2.074 seconds across two runs of each method. Selected timestamps and frame checksums matched. The application uses this faster path for prerecorded files. GPU inference in the integration run was about 28 ms on its last frame; overall throughput also includes decoding, warmup and display. This does not guarantee a particular live-camera frame rate.

## Remaining physical validation

A real camera mount, measured camera profile, time-aligned live GNSS/IMU feed, and surveyed road points are needed to measure absolute distance and positioning accuracy. No physical webcam or external live GNSS device was used in these checks. No Excel workbook was found in the supplied folder; workbook ingestion and matching were tested with controlled fixtures. Source dataset and model files were preserved.
