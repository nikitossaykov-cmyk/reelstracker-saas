"""
API для управления рилсами: CRUD
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.reel import (
    ReelCreate, ReelUpdate, ReelResponse, ReelHistoryResponse,
    ReelMediaUrlResponse, ReelDownloadResponse,
)
from app.services.reel_service import (
    get_user_reels,
    get_reel_by_id,
    create_reel,
    update_reel,
    delete_reel,
    get_reel_history,
)
from app.services.parsing_service import create_parse_job
from app.services.media_service import (
    download_reel_media, get_reel_media_url, delete_reel_media,
)

router = APIRouter()


@router.get("", response_model=List[ReelResponse])
def list_reels(
    include_accounts: bool = Query(False, description="Включать рилсы, импортированные через аккаунты"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Список рилсов юзера. По умолчанию — только вручную добавленные."""
    return get_user_reels(db, current_user, include_accounts=include_accounts)


@router.post("", response_model=ReelResponse, status_code=status.HTTP_201_CREATED)
def add_reel(
    data: ReelCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Добавить новый рилс + поставить в очередь на парсинг"""
    reel = create_reel(db, current_user, data)

    # Сразу ставим на парсинг
    create_parse_job(db, current_user, reel)

    return reel


@router.get("/{reel_id}", response_model=ReelResponse)
def get_reel(
    reel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Получить конкретный рилс"""
    return get_reel_by_id(db, reel_id, current_user)


@router.put("/{reel_id}", response_model=ReelResponse)
def edit_reel(
    reel_id: int,
    data: ReelUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Обновить рилс (title, enabled)"""
    return update_reel(db, reel_id, current_user, data)


@router.delete("/{reel_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_reel(
    reel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удалить рилс"""
    delete_reel(db, reel_id, current_user)


@router.get("/{reel_id}/history", response_model=List[ReelHistoryResponse])
def reel_history(
    reel_id: int,
    limit: int = Query(50, ge=1, le=50000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """История метрик рилса"""
    return get_reel_history(db, reel_id, current_user, limit)


@router.get("/{reel_id}/media-url", response_model=ReelMediaUrlResponse)
def reel_media_url(
    reel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Получить (presigned) URL медиа рилса в нашем R2.

    media_url=null если медиа ещё не скачано (см. POST /download).
    """
    reel = get_reel_by_id(db, reel_id, current_user)
    return ReelMediaUrlResponse(
        media_url=get_reel_media_url(reel),
        storage_key=reel.media_storage_key,
        size_bytes=reel.media_size_bytes,
        downloaded_at=reel.media_downloaded_at,
    )


@router.post("/{reel_id}/download", response_model=ReelDownloadResponse)
def reel_download(
    reel_id: int,
    overwrite: bool = Query(False, description="Перезаписать существующий media если был"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ручной запуск скачивания медиа рилса в наш R2.

    Источник URL — Apify post scraper по shortcode. Требует
    `current_user.apify_token`. IG-CDN URL действителен ~часы.
    """
    reel = get_reel_by_id(db, reel_id, current_user)
    if reel.media_storage_key and not overwrite:
        return ReelDownloadResponse(
            ok=True,
            storage_key=reel.media_storage_key,
            size_bytes=reel.media_size_bytes,
        )

    if not current_user.apify_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нужен apify_token в настройках профиля для получения свежей "
                   "ссылки на медиа (IG-CDN protected). Поставь токен в "
                   "/api/settings/apify, потом повтори.",
        )

    # Дёрнуть Apify по shortcode рилса, чтобы получить свежий videoUrl
    from app.workers.parser_worker import get_parser, _extract_shortcode
    sc = _extract_shortcode(reel.url)
    if not sc:
        raise HTTPException(400, detail="Не удалось извлечь shortcode из reel.url")

    parser = get_parser()
    # fetch_reels_via_apify требует username; берём из автора рилса
    username = reel.author_username
    if not username:
        raise HTTPException(400, detail="reel.author_username пуст — не можем спросить Apify")
    items = parser.fetch_reels_via_apify(username, current_user.apify_token, results_limit=50)
    fresh = next((it for it in items if it.get('shortcode') == sc), None)
    if not fresh or not fresh.get('video_url'):
        return ReelDownloadResponse(
            ok=False,
            error=f"Apify не вернул videoUrl для shortcode {sc} (возможно reel удалён или приватный)",
        )

    key = download_reel_media(db, reel, fresh['video_url'], overwrite=overwrite)
    if key:
        return ReelDownloadResponse(ok=True, storage_key=key, size_bytes=reel.media_size_bytes)
    return ReelDownloadResponse(ok=False, error=reel.media_download_error or "unknown")


@router.delete("/{reel_id}/media", status_code=status.HTTP_204_NO_CONTENT)
def reel_media_delete(
    reel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удалить кэш медиа из R2 + очистить ссылки в БД (метрики и рилс остаются)."""
    reel = get_reel_by_id(db, reel_id, current_user)
    delete_reel_media(db, reel)


@router.post("/{reel_id}/analyze")
def reel_analyze(
    reel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Поставить ANALYZE_REEL задачу в очередь.

    Требует:
    - reel.media_storage_key (т.е. сначала скачать через /download)
    - user.openai_api_key (для Whisper + Vision + classifier)
    """
    reel = get_reel_by_id(db, reel_id, current_user)
    if not reel.media_storage_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Сначала скачай медиа: POST /api/reels/{id}/download",
        )
    if not current_user.openai_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Для анализа нужен openai_api_key в профиле "
                   "(Whisper + Vision API).",
        )
    from app.services.analysis_service import create_analyze_job
    job = create_analyze_job(db, current_user, reel)
    return {"job_id": job.id, "status": job.status.value}
