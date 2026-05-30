"""
Сервис анализа рилсов — ставит ANALYZE_REEL задачи в общую очередь.

Сам worker (analyzer_worker.py) делает heavy lifting через analyzers/.
Тут только enqueue + чтение результатов.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.reel import Reel
from app.models.parsing import ParseJob, JobStatus, JobType
from app.services.tariff_service import get_priority

logger = logging.getLogger(__name__)


def create_analyze_job(db: Session, user: User, reel: Reel) -> ParseJob:
    """Поставить ANALYZE_REEL задачу. Идемпотентно — если уже в очереди / running, вернёт её."""
    existing = db.query(ParseJob).filter(
        ParseJob.reel_id == reel.id,
        ParseJob.job_type == JobType.ANALYZE_REEL,
        ParseJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
    ).first()
    if existing:
        return existing
    job = ParseJob(
        reel_id=reel.id,
        user_id=user.id,
        job_type=JobType.ANALYZE_REEL,
        status=JobStatus.PENDING,
        priority=get_priority(user),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info(f"✅ ANALYZE_REEL #{job.id} queued for reel #{reel.id}")
    return job


def mark_analysis_running(db: Session, reel: Reel) -> None:
    reel.analysis_error = None
    db.commit()


def mark_analysis_done(
    db: Session,
    reel: Reel,
    *,
    transcript: Optional[str] = None,
    visual_summary: Optional[str] = None,
    scenes_json: Optional[str] = None,
    hook_type: Optional[str] = None,
) -> None:
    """Сохранить любые успешные результаты + проставить analyzed_at.

    Аргументы все опциональны — частичный успех тоже валиден (например
    transcript есть, vision API упало).
    """
    if transcript is not None:
        reel.transcript = transcript
    if visual_summary is not None:
        reel.visual_summary = visual_summary
    if scenes_json is not None:
        reel.scenes = scenes_json
    if hook_type is not None:
        reel.hook_type = hook_type
    reel.analyzed_at = datetime.utcnow()
    db.commit()


def mark_analysis_failed(db: Session, reel: Reel, error: str) -> None:
    reel.analysis_error = (error or "")[:2000]
    db.commit()
