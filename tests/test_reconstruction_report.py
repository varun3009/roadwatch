import io
import json
import zipfile

from roadwatch.complaints import complaint_bundle, complaint_html


def test_recovered_photos_keep_reconstruction_provenance_in_report_and_package():
    hazard = {"id": "old:1", "class_name": "Pothole", "latitude": 37, "longitude": -122}
    metadata = {"id": 1, "time_s": 2, "frame_index": 40, "source": "recovered_source_frame",
                "reconstructed": True, "overlay_origin": "current_model_prediction",
                "original_track_association_verified": False,
                "overlays": [{"bbox": [1, 2, 3, 4], "origin": "current_model_prediction"}],
                "note": "Rebuilt prediction, not original tracker output", "source_path": "private/local/file"}
    html = complaint_html(hazard, [(metadata, b"image")])
    assert "Recovered source image; boxes reconstructed with the current model" in html
    with zipfile.ZipFile(io.BytesIO(complaint_bundle(hazard, [(metadata, b"image")]))) as archive:
        evidence = json.loads(archive.read("hazard.json"))["evidence"][0]
        assert evidence["reconstructed"] is True
        assert evidence["original_track_association_verified"] is False
        assert evidence["overlay_origin"] == "current_model_prediction"
        assert "source_path" not in evidence
