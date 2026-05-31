"""Маленький shared helper для R2 upload (используется magic + others)."""

from __future__ import annotations

from pathlib import Path


def upload_to_r2(local: Path, key: str, content_type: str = "video/mp4") -> int:
    """Upload local file to R2 at key. Return size in bytes."""
    from app.core.storage import get_r2
    r2 = get_r2()
    with local.open("rb") as f:
        data = f.read()
    r2.upload_bytes(key, data, content_type=content_type)
    return len(data)
