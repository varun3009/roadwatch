# Hazard evidence, replay and municipal complaints

RoadWatch keeps a persistent local hazard library alongside the current live
session. It uses SQLite; no MongoDB account or server is required. AI detections
are predictions for review. Manual reports are explicitly identified as human
observations, and a downloaded complaint is a draft for the reporter to submit.

## Review saved hazards

Open **Hazard library** to browse records from all sessions. Search by class,
address, title, notes or source, filter by complaint status and paginate through
the results. A hazard detail shows its coordinates and location basis, confidence
when available, source and evidence gallery. Observation counts can exceed the
number of stored images.

Each confirmed tracked hazard saves at most **ten JPEG evidence frames**. During
ordinary live/dataset detection, saved frames are at least **0.5 seconds apart**.
The SQLite write transaction and database trigger enforce the ten-frame cap even
when requests run concurrently. A short-lived hazard or photo submission may
have fewer images. A manual report without an attachment has none.

The library shows photographs first, with filters for all records, records with
photos, and missing photos. **Recover missing photos** extracts real source
frames for older records, in a background job. Each record can also request its
own recovery. The source must still be present locally. A recovered frame is
marked as reconstructed: boxes from a fresh model run are distinguished from
original saved detection evidence. Recovery preserves the original hazard class,
coordinates and complaint details. Records without retained media show a reason
instead of a broken image. Missing-media recovery never invents a photograph.

## Find nearby anomalies

Use **Use my location** in the library, or enter latitude and longitude manually.
Choose a search radius. Records with usable coordinates are sorted by straight
line distance from that point; records without coordinates are excluded from
nearby results. The distance is not a driving route or a guarantee that the road
is accessible. Browser location is optional and subject to browser/device
permission and availability. Manual coordinates provide the fallback.

A nearby match can be an old dataset observation or an approximate camera
location. Review its source, date and quality note before treating it as a
current hazard. Location search does not improve the accuracy of stored points.

## Submit a photo, video or manual report

In **Report a hazard**, choose one of:

- **Detect from a photo or video**: upload evidence and run the configured local
  YOLO model. Supported predictions become saved hazard records. A submission
  with no supported detections reports that outcome and creates no substitute
  hazard.
- **Log a hazard manually**: enter what you observed, including classes outside
  YOLO's training. An attachment is optional. This creates a human report,
  without an AI confidence score or a claim of model verification.

Enter latitude and longitude, or use browser location, and choose the meaning of
the coordinate:

- **Hazard's location** means a point selected or measured by the reporter. It
  remains independently unverified.
- **Camera/vehicle location** means a capture-location proxy. The hazard can be
  some distance from that point. No camera-to-road distance is inferred from
  uploaded GPS alone.

One fixed point applies to the entire video when no GPS track is supplied. This
is useful for an incident at one known location; it does **not** localize hazards
along a moving route. For a moving video, add optional GPS-track JSON with
seconds relative to the start of the video:

```json
[
  {"time_s": 0, "latitude": 24.3757, "longitude": 88.6226, "accuracy_m": 5},
  {"time_s": 1, "latitude": 24.3758, "longitude": 88.6227, "accuracy_m": 5},
  {"time_s": 2, "latitude": 24.3759, "longitude": 88.6228, "accuracy_m": 6}
]
```

Times must increase strictly. The track needs 2-20,000 fixes. Positions are
interpolated only between fixes at most three seconds apart; there is no
extrapolation. Detections outside track coverage or across a longer gap have no
hazard coordinates. A video track describes camera capture positions, not
surveyed hazard centers. Optional `accuracy_m` is the reported GPS accuracy,
which does not establish total hazard-position error. Leave it blank if unknown.

Submission limits are **25 MiB per photo**, **24 megapixels**, **250 MiB per
video** and **five minutes per video**. Video inference samples at most **900
frames** according to a bounded stride. These are local processing limits;
playback frame rate and inference throughput depend on the source and hardware.
Only one upload inference job runs at a time, and live detection must be stopped
before starting one. Submission progress and results are persisted. An
interrupted processing job reports an error after a server restart; already
saved hazards and evidence remain available. Submit the original again to
reprocess it.

Add a title, street/landmark, responsible municipal authority, description and
user-assessed priority where known. These details can be refined in the library
after processing. Priority is a reporter judgement, not an engineering assessment
derived from AI confidence.

## Replay a video

Open **Detection replay** and select a dataset recording or saved video session.
Playing and seeking run YOLO on newly visited frames. Class/confidence boxes are
drawn directly on the video; the synchronized log shows detected hazards and
saved evidence. The library receives those same records and photographs, with
the ten-frame cap still enforced. The hazard timeline can jump to observations.

Prediction metadata for each visited frame is stored in SQLite. Repeating a
frame, seeking backward, or reopening the same source with the same model and
settings reuses detections without increasing observation counts. Nearby frame
overlap associates predictions with hazard tracks; long seeks and occlusions
can create separate identities. This is not a global physical-hazard deduplicator.
Changing the video, model or detection/calibration settings creates a new analysis.

The server decodes raw HEVC too. Playback targets up to **five analyzed preview
frames per second**, has **no audio**, and adapts to model/decoder throughput.
Pause, seek, restart and playback speed controls are available. A paused replay
releases inference ownership on close or after an idle lease, allowing uploads,
live detection and evidence recovery to use the same model hardware safely.
Original videos can be downloaded from `GET /api/replay/video?source_id=...`.

A source ID is `dataset:<recording_id>` or `session:<session_id>`. Original video
replay requires the local source file to remain present. Camera and network
stream sessions retain their capped evidence images; a full stream recording is
not automatically created.

## Prepare a complaint

Open a hazard and review the evidence, source and location quality. Fill in the
title, responsible authority, address/landmark, description and optional reporter
name/contact details. Save them before exporting. The local review statuses are:

| Status | Meaning |
| --- | --- |
| Open | Preparing or reviewing the report |
| Reported | The reporter has submitted it through a separate channel |
| Resolved | The reporter marks the condition resolved |

**Print / save PDF** opens a printable, self-contained HTML complaint draft.
Use the browser's print dialog to save a PDF or print it. **Download evidence
package** downloads a ZIP containing:

- `complaint.html`, with relative links to the packaged photographs;
- `hazard.json`, with coordinates, provenance, evidence timestamps and hashes;
- `hazard.csv`, compatible with Excel;
- up to ten JPEG evidence frames and a short README.

The report includes a map link, supplied description, evidence and uncertainty
notes. It does not invent a municipal authority, reporter identity, engineering
severity or measured accuracy. Dataset replays are identified as historical
evidence: verify that the condition is still present before filing. Enter the
authority yourself and submit through its normal complaint channel. Exporting,
printing and setting a local status do **not** send a message or automatically
submit a complaint. The draft is not a guarantee that a specific municipality
accepts a particular attachment format.

## Storage and API

The database is **`roadwatch_data/roadwatch.sqlite3`**. Hazard evidence JPEGs are
SQLite BLOBs in `hazard_evidence`, linked to hazard records with frame metadata
and SHA-256 hashes. Complaint edits are kept separately so continuing engine
observations do not erase saved review fields. Existing databases migrate in
place and retain earlier records. Original uploaded media lives separately under
`roadwatch_data/uploads/`; session metadata references it for replay.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/library` | Search/filter all saved hazards, optional nearby coordinates/radius, pagination |
| `GET /api/library/{id}` | Hazard, evidence metadata and safe session summary |
| `PATCH /api/library/{id}` | Edit complaint details, severity and review status |
| `GET /api/evidence/{id}.jpg` | Saved JPEG evidence |
| `GET /api/sessions` | Saved session summaries |
| `POST /api/submissions` | Multipart photo/video detection or manual report |
| `GET /api/submissions/{id}` | Processing state and saved submission result |
| `GET /api/replay/sources` | Available local replay sources |
| `GET /api/replay/info?source_id=...` | Duration, dimensions and preview capabilities |
| `GET /api/replay/frame?source_id=...&time_s=...` | One decoded source JPEG |
| `POST /api/replay/analyze` | Open/reuse a persistent detection replay analysis |
| `POST /api/replay/process` | Analyze or reuse a frame, draw boxes and save evidence |
| `POST /api/replay/close` | Release the replay inference lease |
| `GET /api/replay/analysis/{id}` | Saved analysis summary and hazard log |
| `GET /api/library/evidence-status` | Missing/recoverable photograph counts and recovery status |
| `POST /api/library/recover-evidence` | Start a batch recovery of older records |
| `POST /api/library/{id}/recover-evidence` | Recover one hazard's source photographs |
| `GET /api/library/evidence-jobs/{id}` | Recovery progress and per-record results |
| `GET /api/replay/video?source_id=...` | Download original local video |
| `GET /api/library/{id}/complaint.html` | Printable standalone complaint draft |
| `GET /api/library/{id}/complaint.zip` | Self-contained complaint/evidence package |

Library query fields include `search`, `class_name`, `status`, `session_id`,
`latitude`, `longitude`, `radius_m`, `limit` and `offset`. Nearby search requires
both coordinates and returns `nearby_distance_m`. Pagination counts are computed
before applying the page offset. Detail images contain their own evidence URL,
timestamp, frame index and hash when available.
