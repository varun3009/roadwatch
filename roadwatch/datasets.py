"""Read the supplied recordings and import event references without altering sources.

Raw annotations describe events. Isolated anomaly files describe sensor windows;
counting their repeated labels as separate hazards would inflate the reference set.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import math
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


MAX_WORKBOOK_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_WORKBOOK_ENTRIES = 2048
MAX_IMPORT_ROWS = 100_000
MAX_IMPORT_COLUMNS = 256
MAX_REFERENCE_RECORDS = 25_000


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def normalize_hazard(value: Any) -> str:
    """Normalize common model and spreadsheet labels; retain unknown classes."""
    value = str(value or "").strip().casefold()
    key = _key(value)
    if key in {"pothole", "potholes", "roadpothole"}:
        return "pothole"
    if key in {"bump", "bumps", "speedbump", "speedbumps", "speedbreaker"}:
        return "bump"
    if key in {"crack", "cracks", "roadcrack", "roadcracks", "longitudinal", "transverse", "alligator", "longitudinalcrack", "transversecrack", "alligatorcrack"}:
        return "crack"
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def _coordinates(latitude: Any, longitude: Any) -> tuple[float, float] | None:
    lat, lon = _number(latitude), _number(longitude)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 6_371_008.8 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def _stable_id(*values: Any) -> str:
    return hashlib.sha256("|".join(str(value) for value in values).encode()).hexdigest()[:20]


def _sort_id(value: str) -> tuple[int, Any]:
    return (0, int(value)) if value.isdigit() else (1, value.casefold())


def _csv_reader(path: Path):
    stream = path.open("r", encoding="utf-8-sig", newline="")
    return stream, csv.DictReader(stream)


class DatasetCatalog:
    """Lazy, per-recording cache over the Raw Data directory.

    Times are seconds from the sensor recording start. Camera timing alignment
    belongs to the replay configuration and must be measured for each recording.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._recordings: list[dict] | None = None
        self._tracks: dict[str, list[dict]] = {}
        self._times: dict[str, list[float]] = {}
        self._references: dict[str, list[dict]] = {}
        self._issues: set[str] = set()

    def recordings(self) -> list[dict]:
        if self._recordings is None:
            self._recordings = []
            raw = self.root / "Raw Data"
            if not raw.is_dir():
                self._issues.add(f"Raw Data directory not found: {raw}")
                return []
            for directory in sorted((p for p in raw.iterdir() if p.is_dir()), key=lambda p: _sort_id(p.name)):
                video_files = sorted((directory / "Camera").glob("*.mp4"))
                sensor_path = None
                candidates = sorted(directory.glob("*.csv"), key=lambda p: (p.stem != directory.name, p.name))
                for candidate in candidates:
                    try:
                        with candidate.open(encoding="utf-8-sig", newline="") as stream:
                            columns = set(next(csv.reader(stream), []))
                        if {"seconds_elapsed", "location_latitude", "location_longitude"} <= columns:
                            sensor_path = candidate
                            break
                    except (OSError, UnicodeError, csv.Error) as exc:
                        self._issues.add(f"Cannot inspect {candidate}: {exc}")
                annotation = directory / "Annotation.csv"
                self._recordings.append({
                    "id": directory.name,
                    "label": f"Recording {directory.name}" + (" (video unavailable)" if not video_files else ""),
                    "video_path": str(video_files[0]) if video_files else None,
                    "sensor_path": str(sensor_path) if sensor_path else None,
                    "annotation_path": str(annotation) if annotation.is_file() else None,
                })
                if len(video_files) > 1:
                    self._issues.add(f"Recording {directory.name} contains {len(video_files)} videos; the first sorted video is selected.")
        return [dict(item) for item in self._recordings]

    def _recording(self, recording_id: str) -> dict | None:
        return next((item for item in self.recordings() if item["id"] == str(recording_id)), None)

    def _load_track(self, recording_id: str) -> list[dict]:
        recording_id = str(recording_id)
        if recording_id not in self._tracks:
            recording = self._recording(recording_id)
            points: dict[float, dict] = {}
            if recording and recording["sensor_path"]:
                try:
                    stream, reader = _csv_reader(Path(recording["sensor_path"]))
                    with stream:
                        for row in reader:
                            time_s = _number(row.get("seconds_elapsed"))
                            coords = _coordinates(row.get("location_latitude"), row.get("location_longitude"))
                            if time_s is None or time_s < 0 or coords is None:
                                continue
                            heading = _number(row.get("location_bearing"))
                            accuracy = _number(row.get("location_horizontalAccuracy"))
                            speed = _number(row.get("location_speed"))
                            points[time_s] = {
                                "time_s": time_s, "latitude": coords[0], "longitude": coords[1],
                                "heading_deg": heading % 360 if heading is not None and heading >= 0 else None,
                                "accuracy_m": accuracy if accuracy is not None and accuracy >= 0 else None,
                                "speed_mps": speed if speed is not None and speed >= 0 else None,
                            }
                except (OSError, UnicodeError, csv.Error) as exc:
                    self._issues.add(f"Cannot read GPS for recording {recording_id}: {exc}")
            self._tracks[recording_id] = [points[t] for t in sorted(points)]
            self._times[recording_id] = sorted(points)
        return self._tracks[recording_id]

    def gps_track(self, recording_id: str) -> list[dict]:
        return [dict(item) for item in self._load_track(str(recording_id))]

    def pose_at(self, recording_id: str, time_s: float, max_gap_s: float = 3) -> dict | None:
        """Interpolate valid GPS brackets, never extrapolate or bridge long gaps."""
        query = _number(time_s)
        gap_limit = _number(max_gap_s)
        if query is None or gap_limit is None or gap_limit < 0:
            return None
        recording_id = str(recording_id)
        track = self._load_track(recording_id)
        times = self._times[recording_id]
        index = bisect.bisect_left(times, query)
        if index < len(times) and times[index] == query:
            return dict(track[index])
        if index == 0 or index == len(times):
            return None
        before, after = track[index - 1], track[index]
        gap = after["time_s"] - before["time_s"]
        if gap <= 0 or gap > gap_limit:
            return None
        fraction = (query - before["time_s"]) / gap
        result = {"time_s": query}
        for field in ("latitude", "accuracy_m", "speed_mps"):
            left, right = before[field], after[field]
            result[field] = left + (right - left) * fraction if left is not None and right is not None else None
        # Longitude and heading both need a circular interpolation at wraparound.
        result["longitude"] = ((before["longitude"] + ((after["longitude"] - before["longitude"] + 180) % 360 - 180) * fraction + 180) % 360) - 180
        left, right = before["heading_deg"], after["heading_deg"]
        result["heading_deg"] = (left + ((right - left + 180) % 360 - 180) * fraction) % 360 if left is not None and right is not None else None
        return result

    def reference_events(self, recording_id: str | None = None) -> list[dict]:
        ids = [str(recording_id)] if recording_id is not None else [item["id"] for item in self.recordings()]
        events = []
        for identity in ids:
            if identity not in self._references:
                recording = self._recording(identity)
                references, seen = [], set()
                if recording and recording["annotation_path"]:
                    path = Path(recording["annotation_path"])
                    try:
                        stream, reader = _csv_reader(path)
                        with stream:
                            for row in reader:
                                time_s = _number(row.get("seconds_elapsed"))
                                hazard = normalize_hazard(row.get("text"))
                                if time_s is None or time_s < 0 or not hazard:
                                    continue
                                signature = (round(time_s, 3), hazard)
                                if signature in seen:
                                    continue
                                seen.add(signature)
                                pose = self.pose_at(identity, time_s)
                                references.append({
                                    "id": _stable_id("raw", identity, *signature), "recording_id": identity,
                                    "time_s": time_s, "hazard_type": hazard,
                                    "latitude": pose["latitude"] if pose else None,
                                    "longitude": pose["longitude"] if pose else None,
                                    "accuracy_m": pose["accuracy_m"] if pose else None,
                                    "source": str(path),
                                    "location_kind": "vehicle_at_annotation" if pose else "unavailable",
                                })
                    except (OSError, UnicodeError, csv.Error) as exc:
                        self._issues.add(f"Cannot read annotations for recording {identity}: {exc}")
                self._references[identity] = references
            events.extend(dict(item) for item in self._references[identity])
        return events

    def summary(self) -> dict:
        """Metadata audit; does not load all high-rate sensor files."""
        recordings = self.recordings()
        counts: Counter = Counter()
        annotation_rows = 0
        for recording in recordings:
            if not recording["annotation_path"]:
                continue
            try:
                stream, reader = _csv_reader(Path(recording["annotation_path"]))
                with stream:
                    seen = set()
                    for row in reader:
                        hazard, time_s = normalize_hazard(row.get("text")), _number(row.get("seconds_elapsed"))
                        if not hazard or time_s is None or time_s < 0:
                            continue
                        signature = (round(time_s, 3), hazard)
                        if signature not in seen:
                            counts[hazard] += 1
                            annotation_rows += 1
                            seen.add(signature)
            except (OSError, UnicodeError, csv.Error) as exc:
                self._issues.add(f"Cannot summarize {recording['annotation_path']}: {exc}")
        workbook_paths = sorted(str(path) for pattern in ("*.xlsx", "*.xls", "*.xlsm") for path in self.root.glob(pattern))
        # Walk only dataset roots; never traverse virtual environments or caches.
        for dataset_root in (self.root / "Raw Data", self.root / "Isolated Data"):
            if dataset_root.is_dir():
                workbook_paths.extend(str(path) for pattern in ("*.xlsx", "*.xls", "*.xlsm") for path in dataset_root.rglob(pattern))
        issues = set(self._issues)
        if not workbook_paths:
            issues.add("No Excel workbook was found in the supplied dataset. Existing anomaly references come from Annotation.csv; import a workbook to match against external Excel records.")
        missing_videos = sum(item["video_path"] is None for item in recordings)
        if missing_videos:
            issues.add(f"{missing_videos} recording folders have no MP4 video and cannot be replayed.")
        return {
            "root": str(self.root), "recording_count": len(recordings),
            "playable_recording_count": sum(item["video_path"] is not None for item in recordings),
            "sensor_recording_count": sum(item["sensor_path"] is not None for item in recordings),
            "annotation_file_count": sum(item["annotation_path"] is not None for item in recordings),
            "reference_event_count": annotation_rows, "hazard_counts": dict(counts),
            "workbook_count": len(workbook_paths), "workbook_paths": workbook_paths,
            "reference_location_kind": "vehicle_at_annotation",
            "issues": sorted(issues),
        }


_ALIASES = {
    "latitude": ("latitude", "lat", "location_latitude", "gps_latitude", "hazard_latitude"),
    "longitude": ("longitude", "lon", "lng", "long", "location_longitude", "gps_longitude", "hazard_longitude"),
    "hazard_type": ("hazard_type", "type", "class", "label", "anomaly", "anomaly_type", "annotation_text", "text", "hazard"),
    "time_s": ("time_s", "seconds_elapsed", "elapsed_seconds", "timestamp_s", "time", "seconds"),
    "recording_id": ("recording_id", "recording", "recording_number", "recording_no", "video_id", "trip_id"),
    "id": ("id", "event_id", "anomaly_id", "hazard_id"),
    "accuracy_m": ("accuracy_m", "accuracy", "horizontal_accuracy", "location_horizontalAccuracy"),
    "location_kind": ("location_kind", "coordinate_kind"),
    "event_marker": ("annotation_millisecond_press_duration", "millisecond_press_duration"),
}
_MAPPING_KEYS = {"lat": "latitude", "lon": "longitude", "lng": "longitude", "type": "hazard_type", "time": "time_s", "recording": "recording_id", "accuracy": "accuracy_m"}


def validate_reference_mapping(mapping: dict | None) -> dict:
    """Validate user column mappings before parsing or persisting an upload."""
    if mapping is None:
        return {}
    if not isinstance(mapping, dict):
        raise ValueError("Column mapping must be a JSON object")
    result = {}
    for target, source in mapping.items():
        target = _MAPPING_KEYS.get(target, target)
        if target == "sheet":
            if source is not None and (isinstance(source, bool) or not isinstance(source, (str, int)) or isinstance(source, int) and source < 0):
                raise ValueError("Sheet must be a name or a nonnegative integer index")
        elif target not in _ALIASES:
            raise ValueError(f"Unknown reference mapping field: {target}")
        elif not isinstance(source, str) or not source.strip():
            raise ValueError(f"Mapped column for {target} must be a nonempty string")
        if target in result and result[target] != source:
            raise ValueError(f"Conflicting mappings for {target}")
        result[target] = source
    return result


def _column_mapping(columns: list[str], mapping: dict | None) -> dict[str, str]:
    available = {_key(column): column for column in columns}
    resolved = {}
    for target, aliases in _ALIASES.items():
        for alias in aliases:
            if _key(alias) in available:
                resolved[target] = available[_key(alias)]
                break
    for target, source in (mapping or {}).items():
        target = _MAPPING_KEYS.get(target, target)
        if target in _ALIASES and isinstance(source, str):
            # Explicit mappings intentionally override aliases, including invalid ones.
            resolved[target] = available.get(_key(source), source)
    return resolved


def _inferred_recording(path: Path) -> str | None:
    for name in (path.parent.name, path.stem):
        if name.isdigit():
            return name
        match = re.match(r"^(\d+)[_-](?:isolated|combined|anomal)", name, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _deduplicate(records: list[dict], windowed: bool = False) -> list[dict]:
    result, seen, last_by_group = [], set(), {}
    for record in records:
        time_s = record["time_s"]
        group = (record["recording_id"], record["hazard_type"])
        identity = (group, round(time_s, 3) if time_s is not None else None,
                    round(record["latitude"], 7) if record["latitude"] is not None else None,
                    round(record["longitude"], 7) if record["longitude"] is not None else None)
        if identity in seen:
            continue
        seen.add(identity)
        previous = last_by_group.get(group)
        last_by_group[group] = time_s
        # Isolated files forward-fill labels over sensor windows. Collapse each
        # contiguous run, not every 100 Hz row into a separate hazard.
        if windowed and time_s is not None and previous is not None and 0 <= time_s - previous <= 0.25:
            continue
        result.append(record)
    return result


def parse_reference_file(path: Path, mapping: dict | None = None) -> dict:
    """Read CSV or XLSX event references, with explicit optional field mapping.

    ``mapping={"latitude": "GPS North", "longitude": "GPS East",
    "hazard_type": "Defect", "sheet": "Survey"}`` selects custom columns.
    All sheets are read by default. XLSX is opened read-only with data_only=True;
    formulas without cached numeric values are rejected as missing coordinates.
    """
    path = Path(path)
    issues, all_columns, sheets, records = [], [], [], []
    try:
        mapping = validate_reference_mapping(mapping)
    except ValueError as exc:
        return {"records": [], "columns": [], "sheets": [], "issues": [str(exc)]}
    suffix = path.suffix.casefold()
    inferred_recording = _inferred_recording(path)
    workbook = None
    stream = None
    try:
        sources: list[tuple[str, Iterable]] = []
        if suffix in {".csv", ".tsv"}:
            stream = path.open("r", encoding="utf-8-sig", newline="")
            sample = stream.read(4096)
            stream.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = "excel-tab" if suffix == ".tsv" else "excel"
            sources.append((path.name, csv.reader(stream, dialect)))
        elif suffix in {".xlsx", ".xlsm"}:
            # The HTTP compressed-byte limit alone cannot bound XLSX expansion.
            # Inspect metadata before handing any ZIP member to the XML reader.
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_WORKBOOK_ENTRIES or sum(entry.file_size for entry in entries) > MAX_WORKBOOK_EXPANDED_BYTES:
                    raise ValueError("Workbook exceeds the 100 MB expanded-size or 2048 ZIP-entry limit")
            try:
                from openpyxl import load_workbook
            except ImportError:
                return {"records": [], "columns": [], "sheets": [], "issues": ["Install openpyxl to import Excel workbooks."]}
            workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
            selected = mapping.get("sheet")
            worksheets = workbook.worksheets
            if selected is not None and selected != "":
                if isinstance(selected, int):
                    worksheets = [worksheets[selected]] if 0 <= selected < len(worksheets) else []
                else:
                    worksheets = [workbook[str(selected)]] if str(selected) in workbook.sheetnames else []
                if not worksheets:
                    issues.append(f"Selected sheet {selected!r} does not exist.")
            sources.extend((sheet.title, sheet.iter_rows(values_only=True)) for sheet in worksheets)
            for sheet in worksheets:
                if (sheet.max_column or 0) > MAX_IMPORT_COLUMNS or (sheet.max_row or 0) > MAX_IMPORT_ROWS + 1:
                    raise ValueError("Workbook exceeds the 256-column or 100000-row import limit")
        else:
            return {"records": [], "columns": [], "sheets": [], "issues": [f"Unsupported reference format {suffix or '(none)'}. Use CSV or XLSX; convert legacy XLS to XLSX first."]}
        processed_rows = 0
        for sheet_name, rows in sources:
            sheets.append(sheet_name)
            iterator = iter(rows)
            header = next(iterator, ())
            # Ignore initial blank spreadsheet rows, but do not guess arbitrary headers.
            while header and not any(value is not None and str(value).strip() for value in header):
                header = next(iterator, ())
            columns = [str(value).strip() if value is not None else "" for value in header]
            if len(columns) > MAX_IMPORT_COLUMNS:
                raise ValueError("Reference file exceeds the 256-column import limit")
            all_columns.extend(column for column in columns if column and column not in all_columns)
            resolved = _column_mapping(columns, mapping)
            if "hazard_type" not in resolved:
                issues.append(f"{sheet_name}: no hazard type column found; supply a column mapping.")
                continue
            for field, column in resolved.items():
                if column not in columns:
                    issues.append(f"{sheet_name}: mapped column {column!r} for {field} is absent.")
            if any(resolved.get(field) not in columns for field in ("latitude", "longitude")):
                issues.append(f"{sheet_name}: latitude/longitude columns are missing; references can be time matched only within a recording.")
            local_records, marker_records = [], []
            window_previous = {}
            windowed = _key(resolved.get("hazard_type", "")) == "annotationtext"
            for row_number, values in enumerate(iterator, start=2):
                processed_rows += 1
                if processed_rows > MAX_IMPORT_ROWS:
                    raise ValueError("Reference file exceeds the 100000-row import limit")
                row = dict(zip(columns, values))
                hazard = normalize_hazard(row.get(resolved["hazard_type"]))
                if not hazard or hazard in {"none", "nan", "normal", "clean", "clean_road", "no_anomaly", "no_hazard"}:
                    continue
                time_s = _number(row.get(resolved.get("time_s", "")))
                coords = _coordinates(row.get(resolved.get("latitude", "")), row.get(resolved.get("longitude", "")))
                raw_recording = row.get(resolved.get("recording_id", ""))
                if isinstance(raw_recording, float) and raw_recording.is_integer():
                    raw_recording = int(raw_recording)
                recording = str(raw_recording).strip() if raw_recording is not None and str(raw_recording).strip() else inferred_recording
                if time_s is not None and time_s < 0:
                    time_s = None
                if coords is None and not (recording and time_s is not None):
                    if len(issues) < 100:
                        issues.append(f"{sheet_name} row {row_number}: no valid coordinates or recording-relative time; skipped.")
                    elif len(issues) == 100:
                        issues.append("Additional row issues omitted after the first 100.")
                    continue
                accuracy = _number(row.get(resolved.get("accuracy_m", "")))
                explicit_id = row.get(resolved.get("id", ""))
                kind = row.get(resolved.get("location_kind", ""))
                record = {
                    "id": str(explicit_id) if explicit_id is not None and str(explicit_id).strip() else _stable_id(path.name, sheet_name, recording, time_s, hazard, coords),
                    "recording_id": recording, "time_s": time_s, "hazard_type": hazard,
                    "latitude": coords[0] if coords else None, "longitude": coords[1] if coords else None,
                    "accuracy_m": accuracy if accuracy is not None and accuracy >= 0 else None,
                    "source": f"{path}#{sheet_name}",
                    "source_sheet": sheet_name, "source_row": row_number,
                    "location_kind": str(kind).strip() if kind else ("vehicle_at_annotation" if windowed and coords else "reference_coordinate" if coords else "unavailable"),
                }
                if windowed:
                    group = (recording, hazard)
                    previous = window_previous.get(group)
                    window_previous[group] = time_s
                    if time_s is None or previous is None or not 0 <= time_s - previous <= 0.25:
                        local_records.append(record)
                else:
                    local_records.append(record)
                if _number(row.get(resolved.get("event_marker", ""))) is not None:
                    marker_records.append(record)
                if len(records) + max(len(local_records), len(marker_records)) > MAX_REFERENCE_RECORDS:
                    raise ValueError("Reference file exceeds the 25000-event import limit")
            # Marker-bearing rows identify actual button presses inside windows.
            # Without markers, a contiguous annotation run represents one event.
            chosen = marker_records if windowed and marker_records else local_records
            records.extend(_deduplicate(chosen, windowed=windowed and not marker_records))
    except (OSError, UnicodeError, csv.Error, ValueError, KeyError, IndexError) as exc:
        records = []
        issues.append(f"Cannot read reference file: {exc}")
    except Exception as exc:
        records = []
        # Bad ZIP/XML uploads should be a recoverable UI import error.
        issues.append(f"Invalid reference workbook: {exc}")
    finally:
        if workbook is not None:
            workbook.close()
        if stream is not None:
            stream.close()
    records = _deduplicate(records)
    return {"records": records, "columns": all_columns, "sheets": sheets, "issues": issues}


def match_reference(event: dict, references: Iterable[dict], radius_m: float = 12,
                    time_tolerance_s: float = 5, same_recording_only: bool = True) -> dict | None:
    """Find a compatible reference and expose the distance/time evidence.

    If both sides contain coordinates, a spatial match is mandatory; if both
    have times in the same recording, the temporal tolerance is mandatory too.
    References without coordinates require a shared recording and aligned time.
    Unknown recording ids never satisfy strict recording matching.
    """
    radius, tolerance = _number(radius_m), _number(time_tolerance_s)
    if radius is None or radius < 0 or tolerance is None or tolerance < 0:
        return None
    hazard = normalize_hazard(event.get("hazard_type", event.get("class_name")))
    event_recording = str(event["recording_id"]) if event.get("recording_id") is not None else None
    event_time = _number(event.get("time_s"))
    event_coords = _coordinates(event.get("latitude"), event.get("longitude"))
    candidates = []
    for reference in references:
        if not hazard or hazard != normalize_hazard(reference.get("hazard_type")):
            continue
        ref_recording = str(reference["recording_id"]) if reference.get("recording_id") is not None else None
        shared = event_recording is not None and ref_recording is not None and event_recording == ref_recording
        if same_recording_only and not shared:
            continue
        if event_recording is not None and ref_recording is not None and not shared:
            continue
        ref_time = _number(reference.get("time_s"))
        delta = abs(event_time - ref_time) if shared and event_time is not None and ref_time is not None else None
        if delta is not None and delta > tolerance:
            continue
        ref_coords = _coordinates(reference.get("latitude"), reference.get("longitude"))
        distance = _distance_m(*event_coords, *ref_coords) if event_coords and ref_coords else None
        if distance is not None:
            if distance > radius:
                continue
        elif delta is None:
            continue
        score = (distance / max(radius, 1e-9) if distance is not None else 1.0) + (delta / max(tolerance, 1e-9) if delta is not None else 0.0)
        candidates.append((score, {
            "reference_id": reference.get("id"), "distance_m": distance, "time_delta_s": delta,
            "match_kind": "spatial_and_temporal" if distance is not None and delta is not None else "spatial" if distance is not None else "temporal",
            "reference": dict(reference),
        }))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None
