# Dataset and reference matching

The application reads the existing files in place. It does not change the source
videos, sensor files, annotations, or imported workbooks.

## Audit of the supplied files

The dataset at `E:\IS` contains:

| Item | Count |
| --- | ---: |
| Raw recording folders | 102 |
| Recordings with MP4 video | 81 |
| Recordings with combined sensor CSV | 102 |
| Raw `Annotation.csv` files | 101 |
| Distinct annotation events | 688 |
| Pothole events | 292 |
| Bump events | 396 |
| Existing Excel workbooks | 0 |

These counts were verified against the supplied files. The current references
are CSV annotations. Import a CSV or XLSX survey to compare detections with
separate Excel records. Legacy XLS files need conversion to XLSX first. Crack
classes can be detected by the YOLO model and imported as references, but the
provided raw annotations contain only potholes and bumps.

Each recording lives under `Raw Data/<recording id>/`. Video files live in
`Camera/`. The combined sensor CSV has `seconds_elapsed`,
`location_latitude`, and `location_longitude` columns. Its filename varies:
recording 34 uses `1753414336425.csv`. Discovery checks the header rather than
assuming a numbered filename. Folders without videos remain available in the
catalog, with `video_path=None`.

`Isolated Data/Road Anomalies/` contains extracted windows with a label repeated
over many sensor rows. Summary statistics in that directory count labeled rows,
which cannot be used as hazard counts. Recording 34 has **two** pothole button
presses at 4.002 s and 11.901 s; its isolated summary reports 791 labeled rows.
The catalog uses the two events from `Raw Data/34/Annotation.csv`.

## GPS and time alignment

Sensor timestamps are seconds since the start of the recording. `pose_at()`
uses the two valid GPS samples surrounding a requested sensor time. It refuses
to extrapolate before or after the track and refuses to interpolate across a
gap longer than three seconds by default. Invalid, nonfinite, or out-of-range
coordinates are excluded. Missing heading, speed, or accuracy remains `None`.
Heading interpolation follows the short angle around 0/360 degrees; longitude
also handles the international date line.

Camera start time and sensor start time must be aligned through the replay
configuration. The dataset reader does not infer an offset from a filename.
An annotation's interpolated GPS coordinate is the **vehicle location at the
button press** (`location_kind="vehicle_at_annotation"`). It is not an
independent survey of the precise road defect. A camera-projected hazard point
may differ because of the annotation delay, camera geometry, and GPS accuracy.
Those sources of uncertainty must be considered when choosing match tolerances.

For recording 34, the first two interpolated references are approximately:

| Sensor time | Latitude | Longitude | Reported horizontal accuracy |
| ---: | ---: | ---: | ---: |
| 4.002 s | 24.375702069 | 88.622581569 | 3.79 m |
| 11.901 s | 24.375716523 | 88.622768801 | 3.79 m |

## Importing a reference workbook or CSV

`parse_reference_file(path, mapping=None)` returns `records`, `columns`, `sheets`,
and `issues`. It reads CSV/TSV with the standard library and XLSX/XLSM with
`openpyxl` in read-only mode. Workbook formulas require saved cached values:
the importer does not calculate formulas. All sheets are considered by default;
an optional `sheet` mapping selects one sheet by name or zero-based index.

The importer recognizes these common headers, ignoring capitalization and
punctuation:

| Field | Examples |
| --- | --- |
| `latitude` | `latitude`, `lat`, `location_latitude`, `gps_latitude`, `hazard_latitude` |
| `longitude` | `longitude`, `lon`, `lng`, `location_longitude`, `hazard_longitude` |
| `hazard_type` | `hazard_type`, `type`, `class`, `label`, `anomaly_type`, `annotation_text`, `text` |
| `time_s` | `time_s`, `seconds_elapsed`, `elapsed_seconds`, `timestamp_s` |
| `recording_id` | `recording_id`, `recording`, `video_id`, `trip_id` |
| `id` | `id`, `event_id`, `anomaly_id`, `hazard_id` |
| `accuracy_m` | `accuracy_m`, `horizontal_accuracy`, `location_horizontalAccuracy` |
| `location_kind` | `location_kind`, `coordinate_kind` |

Use explicit mappings when column names differ:

```python
from pathlib import Path
from roadwatch.datasets import parse_reference_file

result = parse_reference_file(Path("references.xlsx"), {
    "sheet": "Survey",
    "latitude": "GPS North",
    "longitude": "GPS East",
    "hazard_type": "Defect",
    "recording_id": "Trip",
    "time_s": "Elapsed seconds",
})
```

Time values must be numeric recording-relative seconds; absolute dates and
wall-clock timestamps need conversion before import. A reference needs either
valid latitude/longitude or a recording ID plus a valid elapsed time. Rows
without either are skipped with an issue message. Invalid coordinates are never
silently converted to zero. The importer returns recoverable issues for missing
columns, corrupt files, or unsupported formats.

Generic CSV/XLSX rows are deduplicated by recording, normalized type, time, and
coordinate. Imported sensor windows with `annotation_text` use press-duration
markers to identify events when markers are present; otherwise contiguous
same-type label runs with adjacent timestamps no more than 0.25 seconds apart
collapse to one event. Raw annotation files remain the preferred source.

Pothole/potholes normalize to `pothole`; bump/speed bump normalize to `bump`.
Crack, longitudinal, transverse, and alligator labels normalize to `crack` for
reference matching. This permits a specific model crack class to match a generic
Crack spreadsheet entry; subtype-specific matching is not currently supported.

## Match behavior

`match_reference(event, references, radius_m=12, time_tolerance_s=5,
same_recording_only=True)` requires the same normalized hazard type. When both
sides have coordinates, their great-circle distance must fall within the
radius. When both have elapsed times in the same recording, the time difference
must also fall within the tolerance. Two different explicit recording IDs never
match, even when they describe nearby positions.

The strict default requires matching, nonempty recording IDs. A UI importing
survey points without recording IDs can set `same_recording_only=False` to
allow a spatial match. A point without coordinates can only be matched by time
within the same recording. Results expose the reference ID, distance in meters,
time difference in seconds, and `match_kind` (`spatial`, `temporal`, or
`spatial_and_temporal`). A temporal match alone does not independently validate
the hazard's projected GPS coordinate.

The catalog caches tracks and reference events per recording. `summary()` only
reads metadata and small annotation files; it does not load every high-rate
sensor file. Recreate the catalog after source files change to clear its cache.

## Verification

Run `python -m pytest tests/test_datasets.py -q`. Tests cover sensor discovery,
finite coordinates, GPS gaps and time bounds, heading and longitude wraparound,
event deduplication, the actual recording 34 annotations, read-only XLSX import,
custom mappings, bad files, and matching scope/type/time/distance constraints.

The upload endpoint accepts at most 20 MB of compressed file data. Imports are
also capped at 100 MB of expanded workbook content, 2,048 ZIP entries, 256
columns, 100,000 data rows, and 25,000 reference events. A file exceeding a limit
is rejected as a whole; a partial reference set is not saved. Split larger
surveys into separate files. Mapping field names and value types are validated
before parsing.

Imported references retain the source filename, workbook sheet, source row,
and original row ID. Their application IDs are namespaced by the file content,
mapping, and row evidence, so different workbooks or revised files can safely
reuse IDs such as `1`. Reimporting identical content with the same mapping is
idempotent. A revised file preserves the earlier imported evidence instead of
silently replacing it.
