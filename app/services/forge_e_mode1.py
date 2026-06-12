"""Strategy E, Mode 1 — face-only swap via Replicate.

Donor video and the persona's canonical face image are uploaded as
short-lived public URLs (Replicate requires URL inputs), the face-swap
model is invoked, the result URL is downloaded back to a local path.

The exact Replicate model reference is a spec-§13 open question
(`cdingram/face-swap` is the working default; alternatives need an
eval pass before production).
"""
from __future__ import annotations

from pathlib import Path

from app.services.replicate_client import ReplicateClient
from app.services.media_helpers import download_bytes, upload_temp_public


REPLICATE_MODEL = "cdingram/face-swap"
TEMP_TTL_SECONDS = 3600


def run_mode1(
    *,
    donor: Path,
    face: Path,
    out: Path,
    replicate_api_key: str,
) -> Path:
    donor_url = upload_temp_public(
        donor.read_bytes(),
        suffix=".mp4",
        content_type="video/mp4",
        ttl_seconds=TEMP_TTL_SECONDS,
    )
    face_url = upload_temp_public(
        face.read_bytes(),
        suffix=".png",
        content_type="image/png",
        ttl_seconds=TEMP_TTL_SECONDS,
    )
    client = ReplicateClient(api_key=replicate_api_key)
    result = client.run_model(
        REPLICATE_MODEL,
        {"input_video": donor_url, "swap_image": face_url},
    )
    result_url = result if isinstance(result, str) else result[0]
    blob = download_bytes(result_url)
    out.write_bytes(blob)
    return out
