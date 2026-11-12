"""Local hazard report details and printable incident reports."""
from html import escape
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HazardReport(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(default="", max_length=240)
    address: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=4000)
    severity: Literal["low", "medium", "high"] = "medium"


def _number(value, format_spec):
    if isinstance(value, (int, float)) and isfinite(value):
        return format(value, format_spec)
    return "Unavailable"


def incident_report_html(hazard, evidence):
    hazard_type = hazard.get("class_name") or hazard.get("hazard_type") or "Road hazard"
    title = hazard.get("title") or hazard_type
    source = {"camera": "Live camera", "stream": "Live stream", "file": "Video file", "dataset": "Dataset recording",
              "replay_detection": "Detection replay", "manual": "Manual observation",
              "upload_photo": "Uploaded photo", "upload_video": "Uploaded video"}.get(hazard.get("source_type"), "Saved detection")
    latitude = _number(hazard.get("latitude"), ".6f")
    longitude = _number(hazard.get("longitude"), ".6f")
    coordinates = f"{latitude}, {longitude}" if "Unavailable" not in (latitude, longitude) else "Unavailable"
    location_basis = hazard.get("location_note")
    if not location_basis:
        location_basis = (hazard.get("projection_reason") if coordinates == "Unavailable"
                          else "Estimated from camera calibration and synchronized GPS.")
    time_s = _number(hazard.get("first_video_time_s", hazard.get("first_seen_s")), ".2f")
    distance = _number(hazard.get("distance_m"), ".1f")
    if distance != "Unavailable":
        distance += " m (provisional)" if hazard.get("provisional", True) else " m (estimated)"
    rows = [
        ("Hazard ID", hazard["id"]),
        ("Hazard type", hazard_type),
        ("Source", source),
        ("Recording", hazard.get("recording_id") or "Unavailable"),
        ("First logged (UTC)", hazard.get("created_at") or "Unavailable"),
        ("First seen in footage", time_s + " s" if time_s != "Unavailable" else time_s),
        ("GPS coordinates", coordinates),
        ("Reported GPS accuracy (m)", _number(hazard.get("gps_accuracy_m"), ".1f")),
        ("Estimated distance", distance),
        ("Model confidence", _number(hazard.get("confidence"), ".0%")),
        ("Priority", str(hazard.get("severity", "medium")).capitalize()),
    ]
    details = "".join(f"<div><dt>{label}</dt><dd>{escape(str(value))}</dd></div>" for label, value in rows)
    photos = []
    for number, frame in enumerate(evidence, 1):
        caption = f"Evidence {number}"
        if frame.get("frame_index") is not None:
            caption += f" · Frame {frame['frame_index']}"
        if frame.get("time_s") is not None:
            caption += f" · {frame['time_s']:.2f} s"
        photos.append(f'<figure><img src="/api/evidence/{frame["id"]}.jpg" alt="Hazard evidence {number}">'
                      f"<figcaption>{escape(caption)}</figcaption></figure>")
    photos_html = "".join(photos) or "<p>No evidence frames saved for this hazard.</p>"
    report_status = "Local hazard report" if hazard.get("reported_at") else "Detection record"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Incident report · {escape(str(title))}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #eff4f4; color: #162c35; font: 15px/1.5 system-ui, sans-serif; }}
main {{ width: min(900px, calc(100% - 32px)); margin: 24px auto; padding: clamp(20px, 5vw, 44px);
       background: white; border-top: 5px solid #167366; overflow-wrap: anywhere; }}
header {{ display: flex; justify-content: space-between; flex-wrap: wrap; gap: 12px; align-items: start; }}
header p {{ color: #29685f; font-weight: 650; margin: 0; }}
h1 {{ margin: 0; font-size: 1.9rem; line-height: 1.2; }}
h2 {{ margin: 1.4em 0 .45em; font-size: 1.15rem; }}
dl, .photos {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 245px), 1fr)); gap: 12px 24px; }}
dl {{ background: #f2f7f6; padding: 16px; }}
dt, figcaption {{ font-size: .85em; color: #426166; }}
dd, figure {{ margin: 0; }}
figure {{ break-inside: avoid; }}
img {{ display: block; width: 100%; height: auto; border-radius: 4px; }}
figcaption {{ margin-top: .35em; }}
.notes {{ white-space: pre-wrap; }}
button {{ font: inherit; padding: .45em .85em; background: #166457; color: white;
          border: 0; border-radius: 4px; cursor: pointer; }}
@media print {{
  body {{ background: white; }}
  main {{ width: 100%; margin: 0; padding: 0; }}
  button {{ display: none; }}
  h2 {{ break-after: avoid; }}
}}
</style></head><body><main>
<header><div><p>RoadWatch · {report_status}</p><h1>Incident report</h1></div>
<button type="button" onclick="window.print()">Print / save as PDF</button></header>
<h2>{escape(str(title))}</h2><dl>{details}</dl>
<p>{escape(str(location_basis or 'Location details unavailable.'))}</p>
<h2>Observation details</h2>
<p class="notes">{escape(str(hazard.get('address') or 'Street or landmark unavailable.'))}</p>
<p class="notes">{escape(str(hazard.get('notes') or 'No observation notes added.'))}</p>
<h2>Evidence ({len(photos)})</h2><div class="photos">{photos_html}</div>
</main></body></html>"""
