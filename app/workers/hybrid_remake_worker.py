"""
Worker для REMAKE_VIDEO (hybrid mode) задач.

Берёт GeneratedVideo (со ссылкой на source reel + recipe) → запускает
hybrid_remake_service.execute_hybrid_remake → multi-step cut/gen/concat
→ R2 upload → GV.media_url READY.

Долгая задача: 2-3 Runway calls (1-3 минуты каждый) + ffmpeg ops ≈
5-12 минут общая. Worker блокируется на это время.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.parsing import ParseJob
from app.models.generation import GeneratedVideo
from app.models.user import User
from app.services.parsing_service import complete_job, fail_job
from app.services.generation_service import (
    mark_generation_running, mark_generation_failed,
)
from app.services.hybrid_remake_service import execute_hybrid_remake

logger = logging.getLogger(__name__)


def process_hybrid_remake_job(db: Session, job: ParseJob) -> bool:
    gv = db.query(GeneratedVideo).filter(
        GeneratedVideo.id == job.generated_video_id
    ).first()
    if gv is None:
        fail_job(db, job, "GeneratedVideo not found")
        return True
    user = db.query(User).filter(User.id == job.user_id).first()
    if user is None:
        fail_job(db, job, "User not found")
        mark_generation_failed(db, gv, "User not found")
        return True

    mark_generation_running(db, gv, provider_job_id="hybrid")
    logger.info(f"🧩 HYBRID_REMAKE gv #{gv.id} starting")

    try:
        execute_hybrid_remake(db, gv)
    except Exception as e:
        import traceback
        logger.error(f"hybrid remake gv #{gv.id} failed: {e}\n{traceback.format_exc()}")
        fail_job(db, job, f"hybrid: {str(e)[:300]}")
        mark_generation_failed(db, gv, f"hybrid: {str(e)[:300]}")
        return True

    # execute_hybrid_remake уже сам пометил READY
    complete_job(db, job, 0, 0, 0, 0)
    logger.info(f"✅ HYBRID_REMAKE gv #{gv.id} done")
    return True
