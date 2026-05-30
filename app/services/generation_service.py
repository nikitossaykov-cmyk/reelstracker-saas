"""
Сервис генерации видео — создание задач, чтение статуса, lifecycle.

Не делает реальных HTTP-вызовов к провайдерам — это работа worker-а.
Сервис только пишет в БД + ставит задачу в очередь.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.user import User
from app.models.generation import (
    GeneratedVideo,
    GenerationStatus,
    VideoProvider,
)
from app.models.parsing import ParseJob, JobStatus, JobType
from app.services.tariff_service import get_priority

logger = logging.getLogger(__name__)


def create_generation_job(
    db: Session,
    user: User,
    prompt: str,
    provider: VideoProvider,
    aspect_ratio: str = "9:16",
    duration_seconds: int = 5,
    init_image_url: Optional[str] = None,
    seed: Optional[int] = None,
    model: Optional[str] = None,
) -> GeneratedVideo:
    """Создать GeneratedVideo + поставить GENERATE_VIDEO задачу в общую очередь."""
    provider_params: dict = {
        "aspect_ratio": aspect_ratio,
        "duration_seconds": duration_seconds,
    }
    if init_image_url:
        provider_params["init_image_url"] = init_image_url
    if seed is not None:
        provider_params["seed"] = seed
    if model:
        provider_params["model"] = model

    gv = GeneratedVideo(
        user_id=user.id,
        prompt=prompt,
        provider=provider,
        status=GenerationStatus.PENDING,
        provider_params=provider_params,
    )
    db.add(gv)
    db.flush()  # получаем gv.id

    job = ParseJob(
        reel_id=None,
        user_id=user.id,
        generated_video_id=gv.id,
        job_type=JobType.GENERATE_VIDEO,
        status=JobStatus.PENDING,
        priority=get_priority(user),
    )
    db.add(job)
    db.commit()
    db.refresh(gv)
    logger.info(
        f"✅ Created GeneratedVideo #{gv.id} (provider={provider.value}) "
        f"+ ParseJob #{job.id} GENERATE_VIDEO for user_id={user.id}"
    )
    return gv


def get_user_generations(
    db: Session, user: User, limit: int = 50, offset: int = 0,
) -> tuple[list[GeneratedVideo], int]:
    """Список генераций юзера + общее count."""
    q = db.query(GeneratedVideo).filter(GeneratedVideo.user_id == user.id)
    total = q.with_entities(func.count(GeneratedVideo.id)).scalar() or 0
    items = (
        q.order_by(GeneratedVideo.created_at.desc())
         .limit(limit)
         .offset(offset)
         .all()
    )
    return items, total


def get_generation_by_id(
    db: Session, generation_id: int, user: User,
) -> Optional[GeneratedVideo]:
    return db.query(GeneratedVideo).filter(
        GeneratedVideo.id == generation_id,
        GeneratedVideo.user_id == user.id,
    ).first()


def mark_generation_running(
    db: Session, gv: GeneratedVideo, provider_job_id: str,
) -> None:
    gv.status = GenerationStatus.RUNNING
    gv.provider_job_id = provider_job_id
    if gv.started_at is None:
        gv.started_at = datetime.utcnow()
    db.commit()


def mark_generation_uploading(db: Session, gv: GeneratedVideo) -> None:
    gv.status = GenerationStatus.UPLOADING
    db.commit()


def mark_generation_ready(
    db: Session,
    gv: GeneratedVideo,
    media_url: str,
    media_storage_key: str,
    duration_seconds: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    cost_kopecks: Optional[int] = None,
    thumbnail_url: Optional[str] = None,
) -> None:
    gv.status = GenerationStatus.READY
    gv.media_url = media_url
    gv.media_storage_key = media_storage_key
    gv.duration_seconds = duration_seconds or gv.duration_seconds
    gv.width = width or gv.width
    gv.height = height or gv.height
    gv.cost_kopecks = cost_kopecks if cost_kopecks is not None else gv.cost_kopecks
    gv.thumbnail_url = thumbnail_url or gv.thumbnail_url
    gv.completed_at = datetime.utcnow()
    db.commit()


def mark_generation_failed(db: Session, gv: GeneratedVideo, error_message: str) -> None:
    gv.status = GenerationStatus.FAILED
    gv.error_message = (error_message or "")[:2000]
    gv.completed_at = datetime.utcnow()
    db.commit()
