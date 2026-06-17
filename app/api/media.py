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

from sqlalchemy import String as SAString, cast

from app.database import get_db
from app.models.generation import GeneratedVideo
from app.models.makeugc_job import MakeUGCJob
from app.models.persona import Persona
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
    if has_reel:
        return True
    has_persona = (db.query(Persona)
                   .filter(
                       cast(Persona.gallery_json, SAString).contains(key)
                       | Persona.canonical_face_url.contains(key)
                   )
                   .first() is not None)
    if has_persona:
        return True
    has_makeugc = (db.query(MakeUGCJob)
                   .filter(
                       (MakeUGCJob.product_image_key == key)
                       | (MakeUGCJob.portrait_key == key)
                       | (MakeUGCJob.voiceover_key == key)
                       | (MakeUGCJob.lipsync_key == key)
                       | (MakeUGCJob.output_key == key)
                   )
                   .first() is not None)
    return has_makeugc


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


@router.post("/fix-faststart/{gv_id}")
def fix_faststart(gv_id: int, db: Session = Depends(get_db)):
    """Manually re-pack a specific GV's mp4 with +faststart and re-upload.
    No auth — id is sequential int, exposes only success/error metadata.

    Returns structured debug info: ffmpeg presence, atom layout before/
    after, byte sizes, R2 step results.
    """
    import shutil as _sh
    import subprocess as _sp
    import tempfile
    from pathlib import Path

    gv = db.query(GeneratedVideo).filter(GeneratedVideo.id == gv_id).first()
    if not gv:
        raise HTTPException(404, detail=f"gv #{gv_id} not found")
    if not gv.media_storage_key:
        raise HTTPException(400, detail="gv has no media_storage_key")

    info = {"gv_id": gv_id, "key": gv.media_storage_key}
    info["ffmpeg_in_path"] = bool(_sh.which("ffmpeg"))

    try:
        from app.core.storage import get_r2
        r2 = get_r2()
    except Exception as e:
        info["r2_error"] = str(e)[:200]
        return info

    tmp = Path(tempfile.mkdtemp(prefix="ftfix_"))
    local = tmp / "in.mp4"
    fixed = tmp / "out.mp4"
    try:
        try:
            r2._client.download_file(r2.bucket, gv.media_storage_key, str(local))
            info["downloaded_bytes"] = local.stat().st_size
        except Exception as e:
            info["download_error"] = str(e)[:200]
            return info

        # Probe atom layout before
        from app.core.faststart import is_faststart
        info["is_faststart_before"] = is_faststart(local)
        if info["is_faststart_before"]:
            info["result"] = "already_faststart"
            return info

        # Run ffmpeg directly so we capture stderr
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-i", str(local),
               "-c", "copy", "-movflags", "+faststart",
               str(fixed)]
        try:
            r = _sp.run(cmd, capture_output=True, text=True, timeout=120, check=False)
            info["ffmpeg_rc"] = r.returncode
            info["ffmpeg_stderr"] = (r.stderr or "")[:400]
        except FileNotFoundError as e:
            info["ffmpeg_error"] = f"FileNotFoundError: {e}"
            return info
        except Exception as e:
            info["ffmpeg_exception"] = f"{type(e).__name__}: {e}"
            return info

        if not fixed.exists() or fixed.stat().st_size == 0:
            info["result"] = "ffmpeg_no_output"
            return info
        info["fixed_bytes"] = fixed.stat().st_size
        info["is_faststart_after"] = is_faststart(fixed)

        try:
            with fixed.open("rb") as f:
                r2.upload_bytes(gv.media_storage_key, f.read(), content_type="video/mp4")
            info["result"] = "uploaded"
        except Exception as e:
            info["upload_error"] = str(e)[:200]
            return info

        return info
    finally:
        try:
            for p in (local, fixed):
                p.unlink(missing_ok=True)
            tmp.rmdir()
        except OSError:
            pass


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
            "Cache-Control": "public, max-age=60",
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
            "Cache-Control": "public, max-age=60",
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
        "Cache-Control": "public, max-age=60",
    }
    return StreamingResponse(
        obj["Body"].iter_chunks(chunk_size=64 * 1024),
        status_code=200, headers=headers, media_type=content_type,
    )
