"""
Media proxy — стримит видео байты прямо из R2 через нашу прокси,
не редиректит на presigned URL.

История проблемы:
PR #16-23 пытались отдать видео через 302-redirect на R2 presigned URL.
В curl это работало, но <video> тег в Chrome/Safari/iOS получал 0:00
после redirect — почти наверняка из-за того что R2 не отдаёт CORS
headers нужные video-тегу для cross-origin воспроизведения после
redirect, и тогда содержимое молча отбраковывается.

Этот endpoint вместо redirect качает байты из R2 stream'ом и отдаёт
их клиенту через FastAPI StreamingResponse. Браузер R2 не видит,
никаких CORS issues, Range requests поддерживаются прозрачно.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.generation import GeneratedVideo
from app.models.reel import Reel

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_key_in_db(key: str, db: Session) -> bool:
    """Confirm the key actually belongs to something in our DB so a
    randomly-guessed key can't pull arbitrary objects from the bucket."""
    has_gv = (db.query(GeneratedVideo)
              .filter((GeneratedVideo.media_storage_key == key) |
                      (GeneratedVideo.uniq_storage_key == key))
              .first() is not None)
    if has_gv:
        return True
    has_reel = (db.query(Reel)
                .filter(Reel.media_storage_key == key)
                .first() is not None)
    return has_reel


def _parse_range(header: Optional[str], size: int) -> Optional[tuple[int, int]]:
    """Parse a single 'bytes=start-end' Range header. Returns (start, end)
    inclusive. None if header missing or unparseable."""
    if not header or not header.startswith("bytes="):
        return None
    try:
        spec = header[6:].split(",")[0]
        start_s, end_s = spec.split("-", 1)
        if start_s == "":
            # bytes=-N → last N bytes
            n = int(end_s)
            return max(0, size - n), size - 1
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
        if end >= size:
            end = size - 1
        if start > end:
            return None
        return start, end
    except (ValueError, IndexError):
        return None


@router.get("/diag/{gv_id}")
def diag_gv(gv_id: int, db: Session = Depends(get_db)):
    """Inspector — returns full GV state + R2 head_object metadata."""
    gv = db.query(GeneratedVideo).filter(GeneratedVideo.id == gv_id).first()
    if not gv:
        raise HTTPException(404, detail=f"gv #{gv_id} not found")

    out = {
        "gv_id": gv.id,
        "user_id": gv.user_id,
        "status": gv.status.value if gv.status else None,
        "provider": gv.provider.value if gv.provider else None,
        "media_storage_key": gv.media_storage_key,
        "media_url": gv.media_url,
        "uniq_storage_key": gv.uniq_storage_key,
        "uniq_media_url": gv.uniq_media_url,
        "completed_at": gv.completed_at.isoformat() if gv.completed_at else None,
        "error_message": getattr(gv, "error_message", None),
    }
    if gv.media_storage_key:
        try:
            from app.core.storage import get_r2
            r2 = get_r2()
            head = r2._client.head_object(Bucket=r2.bucket, Key=gv.media_storage_key)
            out["r2_size_bytes"] = head.get("ContentLength")
            out["r2_content_type"] = head.get("ContentType")
            out["r2_last_modified"] = (
                head.get("LastModified").isoformat()
                if head.get("LastModified") else None
            )
        except Exception as e:
            out["r2_error"] = str(e)[:200]
    return out


def _head_response(key: str, db: Session):
    if not _verify_key_in_db(key, db):
        raise HTTPException(404, detail="key not found")
    try:
        from app.core.storage import get_r2
        r2 = get_r2()
        head = r2._client.head_object(Bucket=r2.bucket, Key=key)
    except Exception as e:
        logger.exception("HEAD head_object failed")
        raise HTTPException(502, detail=f"R2 unavailable: {e}")
    size = int(head.get("ContentLength") or 0)
    return Response(
        status_code=200,
        headers={
            "Content-Length": str(size),
            "Content-Type": head.get("ContentType") or "video/mp4",
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
        },
    )


@router.head("")
def media_head(key: str, db: Session = Depends(get_db)):
    return _head_response(key, db)


@router.get("")
def media_stream(key: str, request: Request, db: Session = Depends(get_db)):
    """Stream R2 bytes through our server. Supports Range so HTML5
    <video> can seek and start playback before the full file arrives."""
    if not key or "/" not in key or ".." in key:
        raise HTTPException(400, detail="invalid key")
    if not _verify_key_in_db(key, db):
        raise HTTPException(404, detail="key not found")

    try:
        from app.core.storage import get_r2
        r2 = get_r2()
        head = r2._client.head_object(Bucket=r2.bucket, Key=key)
    except Exception as e:
        logger.exception("GET head_object failed")
        raise HTTPException(502, detail=f"R2 unavailable: {e}")

    size = int(head.get("ContentLength") or 0)
    content_type = head.get("ContentType") or "video/mp4"

    range_header = request.headers.get("range")
    rng = _parse_range(range_header, size)

    s3_kwargs = {"Bucket": r2.bucket, "Key": key}
    if rng:
        start, end = rng
        s3_kwargs["Range"] = f"bytes={start}-{end}"
        try:
            obj = r2._client.get_object(**s3_kwargs)
        except Exception as e:
            logger.exception("ranged get_object failed")
            raise HTTPException(502, detail=f"R2 range: {e}")
        length = end - start + 1
        headers = {
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
        }
        return StreamingResponse(
            obj["Body"].iter_chunks(chunk_size=64 * 1024),
            status_code=206, headers=headers, media_type=content_type,
        )

    try:
        obj = r2._client.get_object(**s3_kwargs)
    except Exception as e:
        logger.exception("get_object failed")
        raise HTTPException(502, detail=f"R2 get: {e}")
    headers = {
        "Content-Length": str(size),
        "Content-Type": content_type,
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
    }
    return StreamingResponse(
        obj["Body"].iter_chunks(chunk_size=64 * 1024),
        status_code=200, headers=headers, media_type=content_type,
    )
