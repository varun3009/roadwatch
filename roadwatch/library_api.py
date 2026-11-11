"""Persistent hazard review, nearby discovery and municipal complaint exports."""
from __future__ import annotations

import math
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from .complaints import complaint_bundle, complaint_filename, complaint_html, coordinates
from .geometry import haversine_m


def _public_session(session):
    if session is None:
        return None
    keys = ("id", "created_at", "status", "source", "source_type", "recording_id", "hazard_count",
            "media_kind", "original_filename", "duration_s", "frame_count", "replay_available", "replay_url",
            "submission_id", "submission_status", "submission")
    return {key: session[key] for key in keys if key in session}


def make_library_router(store, root: Path):
    router = APIRouter()

    def require_hazard(hazard_id):
        hazard = store.hazard(hazard_id)
        if hazard is None:
            raise HTTPException(404, "Hazard not found")
        return hazard

    def report_images(hazard_id):
        images = []
        for evidence in store.evidence(hazard_id)[:10]:
            saved = store.evidence_image(evidence["id"])
            if saved:
                jpeg, metadata = saved
                images.append((metadata, jpeg))
        return images

    @router.get("/api/library")
    def library(search: str = Query("", max_length=500), class_name: str | None = Query(None, max_length=200),
                status: str | None = None, latitude: float | None = Query(None, ge=-90, le=90),
                longitude: float | None = Query(None, ge=-180, le=180), radius_m: float = Query(5000, ge=1, le=1_000_000),
                limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), session_id: str | None = None,
                has_evidence: bool | None = None, evidence_first: bool = True):
        if (latitude is None) != (longitude is None):
            raise HTTPException(422, "Supply both latitude and longitude for nearby search")
        if any(value is not None and not math.isfinite(value) for value in (latitude, longitude, radius_m)):
            raise HTTPException(422, "Nearby coordinates and radius must be finite")
        if status not in (None, "", "all", "open", "reported", "resolved"):
            raise HTTPException(422, "Choose open, reported, resolved or all status")
        all_hazards = store.hazards(session_id)
        counts = {"total": len(all_hazards), "located": sum(coordinates(item) is not None for item in all_hazards),
                  "evidence_frames": sum(item.get("evidence_count", 0) for item in all_hazards)}
        counts.update({value: sum(item.get("status", "open") == value for item in all_hazards) for value in ("open", "reported", "resolved")})
        query = search.strip().casefold()
        found = []
        for item in all_hazards:
            if has_evidence is not None and bool(item.get("evidence_count", 0)) != has_evidence:
                continue
            if status not in (None, "", "all") and item.get("status", "open") != status:
                continue
            if class_name not in (None, "", "all") and str(item.get("class_name", item.get("hazard_type", ""))).casefold() != class_name.casefold():
                continue
            if query and query not in " ".join(str(item.get(field) or "") for field in ("id", "title", "class_name", "hazard_type", "address", "notes", "municipal_authority", "dataset_id", "source_type")).casefold():
                continue
            if latitude is not None:
                point = coordinates(item)
                if point is None:
                    continue
                separation = haversine_m(latitude, longitude, *point)
                if separation > radius_m:
                    continue
                item = {**item, "nearby_distance_m": round(separation, 2)}
            found.append(item)
        if latitude is not None:
            found.sort(key=lambda item: (item["nearby_distance_m"], str(item["id"])))
        elif evidence_first:
            # Stable sorting preserves newest-first order within each group.
            found.sort(key=lambda item: not bool(item.get("evidence_count", 0)))
        # Store.hazards already orders newest first with a deterministic rowid tie.
        return {"hazards": found[offset:offset + limit], "total": len(found), "counts": counts,
                "limit": limit, "offset": offset,
                "classes": sorted({str(item.get("class_name") or item.get("hazard_type") or "Unknown") for item in all_hazards}),
                "nearby": {"latitude": latitude, "longitude": longitude, "radius_m": radius_m} if latitude is not None else None}

    @router.get("/api/library/{hazard_id}")
    def detail(hazard_id: str):
        hazard = require_hazard(hazard_id)
        return {"hazard": hazard, "evidence": store.evidence(hazard_id),
                "session": _public_session(store.session(hazard.get("session_id")))}

    @router.patch("/api/library/{hazard_id}")
    def update(hazard_id: str, changes: dict):
        require_hazard(hazard_id)
        try:
            hazard = store.update_hazard_review(hazard_id, changes)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"hazard": hazard}

    @router.get("/api/evidence/{evidence_id}.jpg")
    def evidence_image(evidence_id: int):
        image = store.evidence_image(evidence_id)
        if image is None:
            raise HTTPException(404, "Evidence image not found")
        jpeg, metadata = image
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable",
                                                               "X-Content-Type-Options": "nosniff",
                                                               "ETag": '"' + metadata["sha256"] + '"'})

    @router.get("/api/sessions")
    def sessions():
        return {"sessions": [_public_session(session) for session in store.sessions()]}

    @router.get("/api/library/{hazard_id}/complaint.html")
    def html_report(hazard_id: str):
        hazard = require_hazard(hazard_id)
        return HTMLResponse(complaint_html(hazard, report_images(hazard_id)), headers={"Cache-Control": "no-store",
                            "Content-Disposition": f'inline; filename="{complaint_filename(hazard_id)}-complaint.html"',
                            "X-Content-Type-Options": "nosniff"})

    @router.get("/api/library/{hazard_id}/complaint.zip")
    def zip_report(hazard_id: str):
        hazard = require_hazard(hazard_id)
        return Response(complaint_bundle(hazard, report_images(hazard_id)), media_type="application/zip",
                        headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="{complaint_filename(hazard_id)}-complaint.zip"'})

    return router
