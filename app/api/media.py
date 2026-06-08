"""
Media proxy — выдаёт свежий R2 presigned URL по 302-redirect.

Зачем: presigned URLs у R2 живут ≤7 дней. Если сохранять их в БД
(`gv.media_url = r2.get_public_url(key)`), то через ~неделю
ссылка протухает с XML-ошибкой ExpiredRequest и видео в UI
перестаёт играть. Этот endpoint решает проблему — UI всегда
обращается к `/api/media?key=...`, а мы под капотом генерим
свежий presigned URL и 302-redirect'им браузер на него.

Безопасность: key содержит UUID (`users/<id>/forge_b/<uuid>.mp4`),
перебрать практически невозможно. Кто-то может скачать чужое видео
только если знает точный storage key — что эквивалентно тому
что эти ссылки и так публичны (presigned URLs тоже).
"""
from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from fastapi import Depends

from app.database import get_db
from app.models.generation import GeneratedVideo
from app.models.reel import Reel

logger = logging.getLogger(__name__)

router = APIRouter()


# HTML5 <video> sends HEAD first for metadata, then GET (with Range) to
# stream. R2 presigned URLs are method-scoped, so we have to mint a
# different URL depending on what the browser asked for — same URL
# signed for GET returns 403 on HEAD and vice versa.
@router.get("/diag/{gv_id}")
def diag_gv(gv_id: int, db: Session = Depends(get_db)):
    """Безопасный inspector — отдаёт все важные поля по GV и состояние
    объекта в R2 (HEAD + GET status). Не требует auth — gv_id это
    предсказуемый int, но кроме metadata ничего вернуть нельзя.
    """
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


@router.head("")
@router.get("")
def media_redirect(key: str, request: Request, db: Session = Depends(get_db)):
    if not key or "/" not in key or ".." in key:
        raise HTTPException(400, detail="invalid key")

    found = (db.query(GeneratedVideo)
             .filter((GeneratedVideo.media_storage_key == key) |
                     (GeneratedVideo.uniq_storage_key == key))
             .first())
    if not found:
        found = (db.query(Reel)
                 .filter(Reel.media_storage_key == key)
                 .first())
    if not found:
        raise HTTPException(404, detail="key not found")

    try:
        from app.core.storage import get_r2
        url = get_r2().get_public_url(key, http_method=request.method)
    except Exception as e:
        logger.exception("media_redirect get_public_url failed")
        raise HTTPException(502, detail=f"R2 unavailable: {e}")
    return RedirectResponse(url, status_code=302)
