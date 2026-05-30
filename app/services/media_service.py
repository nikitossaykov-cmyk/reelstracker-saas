"""
Сервис кэширования медиа рилсов в нашем R2.

Используется content forge pipeline (downloader → analyzer → recipe → remake).
IG-CDN ссылки (`videoUrl` из Apify) протухают за часы, поэтому скачиваем
сразу при sync аккаунта и держим у себя.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.reel import Reel

logger = logging.getLogger(__name__)


def _storage_key_for(reel: Reel) -> str:
    """Структурированный путь в bucket для лёгкого аудита и cleanup-а."""
    salt = uuid.uuid4().hex[:8]
    return f"users/{reel.user_id}/reels/{reel.id}_{salt}.mp4"


def download_reel_media(
    db: Session,
    reel: Reel,
    source_url: str,
    overwrite: bool = False,
) -> Optional[str]:
    """Скачать MP4 рилса по source_url → залить в R2 → апдейтнуть Reel.

    Возвращает storage_key (str) при успехе, None при ошибке.
    Любое исключение ловится и пишется в reel.media_download_error.
    """
    if not source_url:
        return None

    # Уже скачано и не просили перезаписать — выходим
    if reel.media_storage_key and not overwrite:
        return reel.media_storage_key

    # Lazy-import: R2 настроен только когда задан bucket; иначе ошибка
    # должна быть видимой, но не должна валить весь sync.
    try:
        from app.core.storage import get_r2, R2NotConfigured
    except ImportError as e:
        logger.warning(f"media_service: storage import failed: {e}")
        return None

    try:
        r2 = get_r2()
    except R2NotConfigured as e:
        reel.media_download_error = str(e)[:500]
        reel.media_source_url = source_url
        db.commit()
        logger.warning(f"Reel #{reel.id} download skipped — R2 not configured")
        return None

    key = _storage_key_for(reel)
    try:
        _, size_bytes = r2.upload_from_url(source_url, key, content_type="video/mp4",
                                           timeout=120)
    except Exception as e:
        msg = f"download failed: {type(e).__name__}: {str(e)[:200]}"
        reel.media_download_error = msg
        reel.media_source_url = source_url
        db.commit()
        logger.warning(f"Reel #{reel.id} {msg}")
        return None

    reel.media_storage_key = key
    reel.media_size_bytes = size_bytes
    reel.media_downloaded_at = datetime.utcnow()
    reel.media_source_url = source_url
    reel.media_download_error = None
    db.commit()
    logger.info(f"📥 Reel #{reel.id} → {key} ({size_bytes/1024:.0f} KB)")
    return key


def get_reel_media_url(reel: Reel) -> Optional[str]:
    """Вернуть public/presigned URL медиа рилса. None если не скачано."""
    if not reel.media_storage_key:
        return None
    try:
        from app.core.storage import get_r2, R2NotConfigured
        r2 = get_r2()
    except (R2NotConfigured, ImportError):
        return None
    return r2.get_public_url(reel.media_storage_key)


def delete_reel_media(db: Session, reel: Reel) -> bool:
    """Удалить медиа рилса из R2 + очистить ссылки. True при успехе."""
    if not reel.media_storage_key:
        return True
    try:
        from app.core.storage import get_r2
        r2 = get_r2()
        r2.delete(reel.media_storage_key)
    except Exception as e:
        logger.warning(f"Reel #{reel.id} R2 delete failed: {e}")
        return False
    reel.media_storage_key = None
    reel.media_size_bytes = None
    reel.media_downloaded_at = None
    reel.media_download_error = None
    db.commit()
    return True
