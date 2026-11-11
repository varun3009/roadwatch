"""Self-contained municipal complaint drafts. Exporting never submits a report."""
from __future__ import annotations

import base64
import csv
from datetime import datetime, timezone
from html import escape
import io
import json
import math
import re
import zipfile


def complaint_filename(hazard_id):
    return "roadwatch-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(hazard_id))[:100]


def coordinates(hazard):
    lat, lon = hazard.get("latitude"), hazard.get("longitude")
    if (isinstance(lat, bool) or isinstance(lon, bool)
            or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float))
            or not math.isfinite(lat) or not math.isfinite(lon)
            or not -90 <= lat <= 90 or not -180 <= lon <= 180):
        return None
    return float(lat), float(lon)


def complaint_data(hazard, evidence):
    # Deliberately export an allowlist: session config, local file paths and
    # camera-stream credentials never belong in a civic complaint attachment.
    fields = ("id", "session_id", "recording_id", "dataset_id", "source_type", "submission_id",
              "class_name", "hazard_type", "title", "notes", "address", "municipal_authority",
              "reporter_name", "reporter_contact", "severity", "status", "created_at", "updated_at",
              "latitude", "longitude", "coordinate_source", "location_source", "location_reference", "location_method", "location_note", "gps_accuracy_m", "accuracy_note",
              "distance_m", "distance_kind", "provisional", "method", "confidence", "observations",
              "first_seen_s", "last_seen_s", "class_ambiguous", "manual", "review_status")
    return {"report_kind": "municipal_road_hazard_complaint_draft", "submitted": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "hazard": {key: hazard[key] for key in fields if key in hazard},
            "evidence": [{key: image[key] for key in ("id", "time_s", "frame_index", "bbox", "class_name", "confidence", "width", "height", "created_at", "sha256",
                                                     "source", "reconstructed", "overlay_origin", "overlays", "original_track_association_verified", "note") if key in image}
                         for image in evidence[:10]]}


def complaint_html(hazard, images, *, standalone=True):
    """Return printable HTML with embedded JPEGs or ZIP-relative image paths."""
    e = lambda value: escape(str(value if value is not None else "Not supplied"), quote=True)
    title = hazard.get("title") or f"Road hazard inspection request - {hazard.get('class_name') or hazard.get('hazard_type') or 'Road anomaly'}"
    point = coordinates(hazard)
    if point:
        lat, lon = point
        map_url = f"https://www.openstreetmap.org/?mlat={lat:.8f}&mlon={lon:.8f}#map=19/{lat:.8f}/{lon:.8f}"
        location = f'<strong>{lat:.8f}, {lon:.8f}</strong><br><a href="{e(map_url)}">Open this location on a map</a>'
    else:
        location = "Coordinates unavailable - please supply an address or landmark before filing."
    source_kind = hazard.get("source_type") or hazard.get("coordinate_source")
    source = ("Dataset replay: " + str(hazard["dataset_id"])) if hazard.get("dataset_id") else {"upload_video": "Uploaded video", "upload_photo": "Uploaded photo", "manual": "Manual report"}.get(source_kind, str(source_kind or "Saved detection"))
    manual = bool(hazard.get("manual")) or hazard.get("source_type") == "manual" or str(hazard.get("method", "")).startswith("manual")
    claim = "Manually reported observation; please inspect and verify on site." if manual else "Model-assisted hazard prediction; the class and extent require human review."
    if hazard.get("class_ambiguous"):
        claim += " The model produced more than one class for this tracked object."
    if hazard.get("dataset_id"):
        claim += " This evidence comes from a historical dataset replay; confirm the hazard is still present before submitting."
    provenance = (hazard.get("coordinate_source") or hazard.get("location_source")
                  or hazard.get("location_note") or hazard.get("location_method")
                  or ("Projected from synchronized camera/GPS pose" if hazard.get("method") else "Coordinate provenance not supplied"))
    quality = "Coordinates and road distance are provisional estimates." if hazard.get("provisional", True) else "Coordinates and road distance depend on the supplied calibration and GPS."
    if hazard.get("gps_accuracy_m") is not None:
        quality += f" Reported GPS accuracy: {hazard['gps_accuracy_m']} m; this is not total hazard-position accuracy."
    else:
        quality += " Total hazard-position accuracy is unknown."
    if hazard.get("accuracy_note") and hazard["accuracy_note"] != provenance:
        quality += " " + str(hazard["accuracy_note"])
    photos = []
    for number, (metadata, jpeg) in enumerate(images[:10], 1):
        image_src = ("data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")) if standalone else f"evidence/frame-{number:02}.jpg"
        timing = f"Video time {metadata['time_s']:.3f} s" if isinstance(metadata.get("time_s"), (int, float)) else "Uploaded photo"
        frame = f" - frame {metadata['frame_index']}" if metadata.get("frame_index") is not None else ""
        reconstruction = ""
        if metadata.get("reconstructed"):
            basis = {"current_model_prediction": "boxes reconstructed with the current model", "recovered_original_bbox": "stored observation box", "no_original_bbox": "original detection boxes unavailable"}.get(metadata.get("overlay_origin"), "overlay provenance recorded in the evidence metadata")
            reconstruction = f"<br>Recovered source image; {e(basis)}."
        photos.append(f'<figure><img src="{image_src}" alt="Evidence frame {number}"><figcaption>Evidence {number} - {e(timing + frame)}{reconstruction}<br>SHA-256: <span class="hash">{e(metadata.get("sha256", ""))}</span></figcaption></figure>')
    photos_html = "".join(photos) or '<p class="muted">No evidence images were saved for this historical record. Attach a current photograph before filing.</p>'
    generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#edf3f5;color:#173345;font:15px/1.6 Arial,sans-serif}}main{{max-width:900px;margin:28px auto;background:white;padding:48px;border-top:7px solid #098b79}}h1{{font-size:28px;line-height:1.25;margin:10px 0 16px}}h2{{font-size:19px;border-bottom:1px solid #dbe5e9;padding-bottom:8px;margin-top:30px}}p{{margin:10px 0}}.brand{{font-size:12px;letter-spacing:2px;color:#098b79;font-weight:bold}}.muted{{color:#596e7a}}.draft{{background:#fff5db;border-left:4px solid #dfaa35;padding:12px 16px}}.meta{{display:grid;grid-template-columns:1fr 1fr;gap:14px 30px;padding:18px;background:#f4f8f9}}.label{{display:block;font-size:11px;font-weight:bold;text-transform:uppercase;letter-spacing:.8px;color:#596e7a}}.multiline{{white-space:pre-wrap;overflow-wrap:anywhere}}a{{color:#087f70}}.actions{{max-width:900px;margin:22px auto;padding:0 4px}}button{{background:#098b79;border:0;color:white;padding:12px 18px;border-radius:6px;font-weight:bold;cursor:pointer}}.photos{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}figure{{margin:0;break-inside:avoid;border:1px solid #dbe5e9;padding:10px}}figure img{{width:100%;height:auto;display:block}}figcaption{{font-size:11px;line-height:1.5;margin-top:8px}}.hash{{overflow-wrap:anywhere;word-break:break-all}}footer{{font-size:11px;margin-top:32px;border-top:1px solid #dbe5e9;padding-top:12px}}@media(max-width:650px){{main{{margin:0;padding:24px}}.meta,.photos{{grid-template-columns:1fr}}.actions{{padding:0 20px}}}}@media print{{@page{{size:A4;margin:15mm}}body{{background:white;font-size:10pt}}main{{margin:0;padding:10px;border-top-width:4px}}.actions{{display:none}}h1{{font-size:21pt}}h2{{font-size:14pt;break-after:avoid}}.meta{{gap:10px 20px}}figure{{max-width:100%}}a{{color:#173345;text-decoration:none}}}}
</style></head><body><div class="actions"><button type="button" onclick="window.print()">Print / save as PDF</button> <span class="muted">Review details, then attach this draft to your municipality's complaint form.</span></div><main>
<div class="brand">ROADWATCH / CIVIC ROAD REPORT</div><h1>{e(title)}</h1><div class="draft"><strong>Complaint draft - not submitted</strong><br>This document records evidence for an inspection request. Downloading or printing does not notify any municipal authority.</div>
<h2>Inspection request</h2><p>To: <strong>{e(hazard.get('municipal_authority') or 'Municipal road maintenance department - authority to be supplied')}</strong></p><p>Please inspect the road anomaly described below and arrange appropriate maintenance or safety measures after verifying the condition on site.</p>
<div class="meta"><div><span class="label">Report reference</span>{e(hazard['id'])}</div><div><span class="label">Hazard / review status</span>{e(hazard.get('class_name') or hazard.get('hazard_type') or 'Road anomaly')} / {e(hazard.get('status', 'open'))}</div><div><span class="label">Coordinates</span>{location}</div><div><span class="label">Address / landmark</span><span class="multiline">{e(hazard.get('address') or 'Not supplied')}</span></div><div><span class="label">User-assessed priority</span>{e(hazard.get('severity', 'medium')).capitalize()}</div><div><span class="label">First logged (UTC)</span>{e(hazard.get('created_at') or 'Unknown')}</div></div>
<h2>Description</h2><p class="multiline">{e(hazard.get('notes') or 'A road-surface anomaly was recorded at the location above. Please review the evidence and verify the required repair.')}</p><p>{e(claim)}</p>
<h2>Evidence provenance and location quality</h2><p><strong>Source:</strong> {e(source)}<br><strong>Coordinate basis:</strong> {e(provenance)}</p><p>{e(quality)}</p><p class="muted">AI confidence is a model score, not a measured probability or an engineering severity assessment. Please review the images and current road condition before filing.</p>
<h2>Supporting photographs ({len(images[:10])} / 10 maximum)</h2><div class="photos">{photos_html}</div>
<h2>Reporter details</h2><p><strong>Name:</strong> {e(hazard.get('reporter_name') or 'Not supplied')}<br><strong>Contact:</strong> {e(hazard.get('reporter_contact') or 'Not supplied')}</p><p>Please provide an acknowledgement or complaint reference after submission through your municipality's normal channel.</p>
<footer>Generated {e(generated)}. Keep the evidence originals and verify the location before filing. Report status is managed locally by the reporter.</footer></main></body></html>'''


def complaint_bundle(hazard, images):
    output = io.BytesIO()
    report = complaint_data(hazard, [metadata for metadata, _ in images])
    flat = report["hazard"]
    csv_output = io.StringIO(newline="")
    writer = csv.DictWriter(csv_output, fieldnames=list(flat))
    writer.writeheader()
    # Neutralize formula-leading user text when opened in Excel.
    writer.writerow({key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value for key, value in flat.items()})
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("complaint.html", complaint_html(hazard, images, standalone=False))
        archive.writestr("hazard.json", json.dumps(report, indent=2, allow_nan=False))
        archive.writestr("hazard.csv", "\ufeff" + csv_output.getvalue())
        archive.writestr("README.txt", "ROADWATCH MUNICIPAL COMPLAINT DRAFT\n\nOpen complaint.html in a browser to review and print/save as PDF.\nThe evidence/ directory contains at most ten JPEG frames.\nGPS coordinates and AI predictions may be provisional.\nThis package has not been submitted to any authority.\nVerify the current road condition and use the municipality's official complaint channel.\n")
        for number, (_, jpeg) in enumerate(images[:10], 1):
            archive.writestr(f"evidence/frame-{number:02}.jpg", jpeg)
    return output.getvalue()
