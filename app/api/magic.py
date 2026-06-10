"""
Magic Mode API — один-click pipeline «дай URL → получи ремейк».

Минимум кнопок, максимум магии. Старые эндпоинты (recipes, remakes)
остаются для продвинутого использования.
"""

from typing import Optional
from pathlib import Path
import tempfile
import uuid

from fastapi import APIRouter, Depends, HTTPException, status, File, UploadFile, Form
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.generation import VideoProvider

router = APIRouter()


class MagicFromUrlRequest(BaseModel):
    source_url: str = Field(min_length=10, max_length=2048)
    brand: Optional[str] = Field(None, max_length=255)
    product_description: Optional[str] = Field(None, max_length=1000)
    extra_instructions: Optional[str] = Field(None, max_length=2000)
    provider: VideoProvider = VideoProvider.RUNWAY
    model: Optional[str] = Field(None, max_length=64)
    duration_seconds: int = Field(5, ge=1, le=30)


class MagicStartResponse(BaseModel):
    reel_id: int
    magic_account_id: int
    source_title: Optional[str] = None
    next_step: str = "GET /api/magic/{reel_id}/status"


class MagicStatusResponse(BaseModel):
    reel_id: int
    status: str  # uploaded | analyzing | analyzed | recipe_ready | generating | ready | failed
    source_url: Optional[str] = None
    source_title: Optional[str] = None
    detail: dict = {}
    where: Optional[str] = None  # если failed — на какой стадии
    error: Optional[str] = None


@router.post("/from-url", response_model=MagicStartResponse, status_code=status.HTTP_202_ACCEPTED)
def magic_from_url(
    data: MagicFromUrlRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Запустить magic pipeline по URL TikTok / Instagram Reels / YouTube Shorts.

    Pre-requisites:
    - user.runway_api_key (для генерации)
    - user.openai_api_key (для analyzer + recipe extractor)

    Возвращает reel_id; поллите GET /api/magic/{reel_id}/status каждые 5-15 сек.
    """
    if not current_user.runway_api_key:
        raise HTTPException(400, detail="Нужен runway_api_key в профиле (для генерации ремейка)")
    if not current_user.openai_api_key:
        raise HTTPException(400, detail="Нужен openai_api_key в профиле (для analyzer и recipe)")
    from app.services.magic_service import start_magic_from_url
    try:
        result = start_magic_from_url(
            db, current_user,
            source_url=data.source_url,
            brand=data.brand,
            product_description=data.product_description,
            extra_instructions=data.extra_instructions,
            provider=data.provider,
            model=data.model,
            duration_seconds=data.duration_seconds,
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return MagicStartResponse(
        reel_id=result["reel_id"],
        magic_account_id=result["magic_account_id"],
        source_title=(result.get("source_meta") or {}).get("title"),
    )


@router.post("/from-upload", response_model=MagicStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def magic_from_upload(
    video: UploadFile = File(...),
    brand: Optional[str] = Form(None),
    product_description: Optional[str] = Form(None),
    extra_instructions: Optional[str] = Form(None),
    duration_seconds: int = Form(5),
    model: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Magic pipeline по uploaded MP4. То же что from-url но без yt-dlp."""
    if not current_user.runway_api_key or not current_user.openai_api_key:
        raise HTTPException(400, detail="Нужны runway_api_key и openai_api_key в профиле")
    # save to tmp
    workdir = Path(tempfile.mkdtemp(prefix=f"magic_up_{current_user.id}_"))
    fp = workdir / f"upload{Path(video.filename or 'v.mp4').suffix}"
    fp.write_bytes(await video.read())

    # Upload to R2
    from app.core.media_service_helpers import upload_to_r2
    key = f"users/{current_user.id}/magic_source/{uuid.uuid4().hex[:12]}.mp4"
    size = upload_to_r2(fp, key)
    try: fp.unlink(missing_ok=True); workdir.rmdir()
    except OSError: pass

    # Create Reel + analyze + chain (same as from-url, just no yt-dlp)
    from app.models.reel import Reel
    from app.models.account import InstagramAccount
    from app.services.analysis_service import create_analyze_job
    from datetime import datetime

    remake_params = {k: v for k, v in {
        "brand": brand, "product_description": product_description,
        "extra_instructions": extra_instructions,
        "duration_seconds": duration_seconds, "model": model,
    }.items() if v is not None}

    magic_acc = (db.query(InstagramAccount)
                 .filter(InstagramAccount.user_id == current_user.id,
                         InstagramAccount.instagram_username == "__magic_mode__")
                 .first())
    if not magic_acc:
        magic_acc = InstagramAccount(
            user_id=current_user.id, instagram_username="__magic_mode__",
            full_name="Magic Mode", sync_enabled=False,
            auto_analyze_media=True, auto_remake_enabled=True,
            auto_uniqify=True, default_remake_params=remake_params,
            viral_growth_threshold=999.0,
        )
        db.add(magic_acc); db.commit(); db.refresh(magic_acc)
    else:
        magic_acc.default_remake_params = remake_params
        magic_acc.auto_analyze_media = True
        magic_acc.auto_remake_enabled = True
        db.commit()

    reel = Reel(
        user_id=current_user.id, instagram_account_id=magic_acc.id,
        title=(video.filename or "uploaded magic")[:255],
        platform="upload", url=f"local://upload_{key}", enabled=False,
        media_storage_key=key, media_size_bytes=size,
        media_downloaded_at=datetime.utcnow(),
    )
    db.add(reel); db.commit(); db.refresh(reel)
    create_analyze_job(db, current_user, reel)
    return MagicStartResponse(
        reel_id=reel.id, magic_account_id=magic_acc.id,
        source_title=video.filename,
    )


@router.get("/{reel_id}/status", response_model=MagicStatusResponse)
def magic_status_endpoint(
    reel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Прогресс magic-job. UI поллит каждые 5-15 сек.

    Stages:
      uploaded → analyzing → analyzed → recipe_ready → generating → ready
    """
    from app.services.magic_service import magic_status
    result = magic_status(db, current_user, reel_id)
    if result.get("status") == "not_found":
        raise HTTPException(404, detail=f"Magic job для reel #{reel_id} не найден")
    return MagicStatusResponse(**result)
