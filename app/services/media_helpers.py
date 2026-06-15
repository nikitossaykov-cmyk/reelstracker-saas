"""Thin helpers for Strategy E worker.

The existing code has app.core.storage (R2 client) and
app.core.yt_downloader.download_video — but a couple of small primitives
are missing that the persona worker and Strategy E need:

- download_bytes(url) — plain GET, returns response.content. Used to
  pull a Replicate result URL or a persona's canonical face image.
- upload_temp_public(blob, ...) — short-lived public R2 URL for handing
  donor video / face image to Replicate (which requires URL inputs).

Both are intentionally tiny so the surface to mock in tests is obvious.
"""
from __future__ import annotations

import uuid


DEFAULT_TEMP_PREFIX = "_tmp"


def download_bytes(url: str, timeout: int = 60) -> bytes:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    if parsed.path.endswith("/api/media"):
        key = parse_qs(parsed.query).get("key", [None])[0]
        if key:
            from app.core.storage import get_r2
            r2 = get_r2()
            obj = r2._client.get_object(Bucket=r2.bucket, Key=key)
            return obj["Body"].read()

    import requests
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


def upload_temp_public(
    blob: bytes,
    *,
    suffix: str = ".bin",
    content_type: str = "application/octet-stream",
    ttl_seconds: int = 3600,
) -> str:
    """Upload blob to R2 under a short-lived temp key, return a
    presigned GET URL valid for ttl_seconds."""
    from app.core.storage import get_r2

    r2 = get_r2()
    key = f"{DEFAULT_TEMP_PREFIX}/{uuid.uuid4().hex}{suffix}"
    r2.upload_bytes(key, blob, content_type=content_type)
    # the R2 helper exposes get_public_url with a method-aware signing
    # path; for Replicate inputs we want a GET-signed URL specifically.
    try:
        return r2.get_public_url(key, http_method="GET")
    except TypeError:
        # legacy single-arg variant
        return r2.get_public_url(key)
