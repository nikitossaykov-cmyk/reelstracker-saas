"""
Сервис uniqification GeneratedVideo — скачать оригинал из R2 → пропустить
через ffmpeg preset → залить uniq-копию обратно → апдейтнуть БД.

Синхронно (один ролик 5-15 сек видео обрабатывается за ~10-30 сек на
обычном CPU). Если будет много трафика — переносим в JobType.UNIQIFY.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models.generation import GeneratedVideo
from app.core.uniqualizer import uniqify_video, UniqifyError, UniqifyPreset

logger = logging.getLogger(__name__)


def uniqify_generated_video(
    db: Session,
    gv: GeneratedVideo,
    preset: Optional[UniqifyPreset] = None,
    randomise: bool = True,
    seed: Optional[int] = None,
    overwrite: bool = False,
) -> str:
    """Сделать uniq-копию gv.media_storage_key → загрузить как uniq_storage_key.

    Возвращает presigned URL копии. Бросает UniqifyError при ошибках.
    """
    if not gv.media_storage_key:
        raise UniqifyError(f"gv #{gv.id} has no media_storage_key — generate first")
    if gv.uniq_storage_key and not overwrite:
        # Уже uniq-нуто — возвращаем существующий URL (proxy → fresh presigned)
        from app.core.storage import get_r2
        return get_r2().get_proxy_url(gv.uniq_storage_key)

    try:
        from app.core.storage import get_r2
        r2 = get_r2()
    except Exception as e:
        raise UniqifyError(f"R2 not available: {e}")

    workdir = Path(tempfile.mkdtemp(prefix=f"uniq_{gv.id}_"))
    src = workdir / "src.mp4"
    dst = workdir / "uniq.mp4"
    try:
        r2._client.download_file(r2.bucket, gv.media_storage_key, str(src))
        uniqify_video(src, dst, preset=preset, randomise=randomise, seed=seed)
        uniq_key = (
            f"users/{gv.user_id}/uniq/{gv.id}_{uuid.uuid4().hex[:8]}.mp4"
        )
        with dst.open("rb") as f:
            r2.upload_bytes(uniq_key, f.read(), content_type="video/mp4")
        uniq_url = r2.get_proxy_url(uniq_key)

        gv.uniq_storage_key = uniq_key
        gv.uniq_media_url = uniq_url
        gv.uniqified_at = datetime.utcnow()
        db.commit()
        logger.info(f"✅ uniq gv #{gv.id} → {uniq_key}")
        return uniq_url
    finally:
        for p in (src, dst):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass
