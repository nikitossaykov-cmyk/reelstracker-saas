"""
Worker для GENERATE_VIDEO задач — провайдер → polling → R2 upload.

Архитектура совпадает с parser_worker: одна задача за вызов, sync-логика
(внутри отдельного треда из lifespan). Долгая часть — polling провайдера
— блокирует тред на время. Это ОК для MVP при невысокой нагрузке; при
росте — выносить в отдельный процесс / Celery-style.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.models.parsing import ParseJob
from app.models.generation import GeneratedVideo
from app.models.user import User
from app.services.parsing_service import complete_job, fail_job
from app.services.generation_service import (
    mark_generation_running,
    mark_generation_uploading,
    mark_generation_ready,
    mark_generation_failed,
)
from app.core.video_providers import (
    get_provider,
    GenerationRequest,
    ProviderJobStatus,
    ProviderError,
)
from app.core.video_providers.base import VideoProviderBase
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _provider_for(gv: GeneratedVideo, user: User) -> VideoProviderBase:
    """Инстанцировать провайдер на основе GeneratedVideo.provider + ключа юзера."""
    provider_name = gv.provider.value
    if provider_name == "mock":
        return get_provider("mock")
    if provider_name == "runway":
        if not user.runway_api_key:
            raise ProviderError("Юзер не задал runway_api_key — нечем авторизоваться")
        return get_provider("runway", api_key=user.runway_api_key)
    raise ProviderError(f"Provider '{provider_name}' пока не интегрирован")


def _r2_key_for(user_id: int, generation_id: int) -> str:
    """Структурированный путь в bucket-е чтобы легко найти и cleanup-ить."""
    salt = uuid.uuid4().hex[:8]
    return f"users/{user_id}/generated/{generation_id}_{salt}.mp4"


def process_generate_video_job(db: Session, job: ParseJob) -> bool:
    """Обработать один GENERATE_VIDEO job.

    Возвращает True (job обработан) всегда — успех/неуспех зафиксированы
    в БД через complete_job / fail_job.
    """
    gv = db.query(GeneratedVideo).filter(
        GeneratedVideo.id == job.generated_video_id
    ).first()
    if gv is None:
        fail_job(db, job, "GeneratedVideo не найден")
        return True

    user = db.query(User).filter(User.id == job.user_id).first()
    if user is None:
        fail_job(db, job, "User не найден")
        mark_generation_failed(db, gv, "User не найден")
        return True

    logger.info(f"🎬 GENERATE_VIDEO #{job.id} → gv #{gv.id} (provider={gv.provider.value})")

    # 1) Инстанцировать провайдер
    try:
        provider = _provider_for(gv, user)
    except ProviderError as e:
        logger.error(f"Provider init failed for gv #{gv.id}: {e}")
        fail_job(db, job, str(e))
        mark_generation_failed(db, gv, str(e))
        return True

    # 2) Сабмит
    params = gv.provider_params or {}
    request = GenerationRequest(
        prompt=gv.prompt,
        init_image_url=params.get("init_image_url"),
        duration_seconds=int(params.get("duration_seconds")
                             or settings.RUNWAY_DEFAULT_DURATION),
        aspect_ratio=str(params.get("aspect_ratio") or "9:16"),
        seed=params.get("seed"),
        extra={"model": params.get("model")} if params.get("model") else {},
    )
    try:
        submit_result = provider.submit(request)
    except ProviderError as e:
        logger.error(f"Provider submit failed for gv #{gv.id}: {e}")
        fail_job(db, job, str(e))
        mark_generation_failed(db, gv, str(e))
        return True

    mark_generation_running(db, gv, submit_result.provider_job_id)
    logger.info(f"  submitted → provider_job_id={submit_result.provider_job_id}")

    # 3) Polling
    deadline = time.time() + settings.RUNWAY_POLL_TIMEOUT_SECONDS
    last_status: Optional[ProviderJobStatus] = None
    final = None
    while time.time() < deadline:
        time.sleep(settings.RUNWAY_POLL_INTERVAL_SECONDS)
        try:
            poll_result = provider.poll(submit_result.provider_job_id)
        except ProviderError as e:
            logger.error(f"Provider poll failed for gv #{gv.id}: {e}")
            fail_job(db, job, str(e))
            mark_generation_failed(db, gv, str(e))
            return True
        if poll_result.status != last_status:
            logger.info(f"  status: {poll_result.status.value}")
            last_status = poll_result.status
        if poll_result.status == ProviderJobStatus.SUCCEEDED:
            final = poll_result
            break
        if poll_result.status in (ProviderJobStatus.FAILED, ProviderJobStatus.CANCELLED):
            err = poll_result.error_message or poll_result.status.value
            fail_job(db, job, err)
            mark_generation_failed(db, gv, err)
            return True

    if final is None:
        err = (f"Provider timeout: задача не завершилась за "
               f"{settings.RUNWAY_POLL_TIMEOUT_SECONDS}s")
        fail_job(db, job, err)
        mark_generation_failed(db, gv, err)
        return True

    # 4) Скачать → R2
    mark_generation_uploading(db, gv)
    try:
        from app.core.storage import get_r2, R2NotConfigured
        try:
            r2 = get_r2()
        except R2NotConfigured as e:
            fail_job(db, job, str(e))
            mark_generation_failed(db, gv, str(e))
            return True
        key = _r2_key_for(user.id, gv.id)
        _, size_bytes = r2.upload_from_url(final.media_url, key)
        public_url = r2.get_public_url(key)
        logger.info(f"  uploaded to R2: {key} ({size_bytes} bytes) → {public_url[:60]}...")
    except Exception as e:
        logger.error(f"R2 upload failed for gv #{gv.id}: {e}")
        fail_job(db, job, f"R2 upload: {e}")
        mark_generation_failed(db, gv, f"R2 upload: {e}")
        return True

    # 5) Финал
    cost_kopecks = None
    if final.cost_usd_cents is not None:
        # 1 USD-cent ≈ 0.9 ₽ at the time of writing; точный курс — отдельная задача.
        # Сохраняем как есть в копейках USD, преобразование в RUB сделает биллинг.
        cost_kopecks = final.cost_usd_cents
    mark_generation_ready(
        db, gv,
        media_url=public_url,
        media_storage_key=key,
        duration_seconds=final.duration_seconds,
        width=final.width,
        height=final.height,
        cost_kopecks=cost_kopecks,
    )
    complete_job(db, job, 0, 0, 0, 0)
    logger.info(f"✅ Generation #{gv.id} READY → {public_url[:60]}...")
    return True
