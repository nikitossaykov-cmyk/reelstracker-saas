"""
Worker для ANALYZE_REEL задач.

Pipeline: качаем медиа из R2 в /tmp → transcribe + vision summary +
detect scenes (параллельно? пока последовательно для простоты) →
classify hook → сохраняем в Reel.

Каждый шаг изолирован: если transcriber упал — vision всё равно
попробует, и результат частично сохранится. Worker fail-ит задачу
только если вообще ничего не получилось.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models.parsing import ParseJob
from app.models.reel import Reel
from app.models.user import User
from app.services.parsing_service import complete_job, fail_job
from app.services.analysis_service import (
    mark_analysis_running,
    mark_analysis_done,
    mark_analysis_failed,
)
from app.core.analyzers import (
    transcribe_audio, TranscribeError,
    summarize_video, VisionError,
    detect_scenes, SceneError,
    classify_hook, ClassifyError,
)

logger = logging.getLogger(__name__)


def _download_media_to_tmp(reel: Reel) -> Optional[Path]:
    """Скачать reel.media_storage_key из R2 в /tmp/."""
    if not reel.media_storage_key:
        return None
    try:
        from app.core.storage import get_r2
        r2 = get_r2()
    except Exception as e:
        logger.warning(f"R2 not available for reel #{reel.id}: {e}")
        return None

    tmp = Path(tempfile.mkdtemp(prefix="reel_")) / f"reel_{reel.id}.mp4"
    try:
        # boto3 download_file прямо в local path
        r2._client.download_file(r2.bucket, reel.media_storage_key, str(tmp))
    except Exception as e:
        logger.warning(f"R2 download failed for reel #{reel.id}: {e}")
        return None
    return tmp


def _cleanup(media_path: Optional[Path]) -> None:
    if not media_path:
        return
    try:
        media_path.unlink(missing_ok=True)
        media_path.parent.rmdir()
    except OSError:
        pass


def process_analyze_reel_job(db: Session, job: ParseJob) -> bool:
    """Обработать один ANALYZE_REEL job."""
    reel = db.query(Reel).filter(Reel.id == job.reel_id).first()
    if reel is None:
        fail_job(db, job, "Reel не найден")
        return True

    user = db.query(User).filter(User.id == job.user_id).first()
    if user is None:
        fail_job(db, job, "User не найден")
        return True

    if not user.openai_api_key:
        msg = ("user.openai_api_key пуст — без него Whisper/Vision/classifier "
               "не работают. Поставь ключ в настройках профиля.")
        fail_job(db, job, msg)
        mark_analysis_failed(db, reel, msg)
        return True

    if not reel.media_storage_key:
        msg = ("reel.media_storage_key пуст — сначала скачай медиа в R2 "
               "(см. /api/reels/{id}/download или auto_download_media).")
        fail_job(db, job, msg)
        mark_analysis_failed(db, reel, msg)
        return True

    logger.info(f"🔍 ANALYZE_REEL #{job.id} → reel #{reel.id}")
    mark_analysis_running(db, reel)

    media_path = _download_media_to_tmp(reel)
    if not media_path:
        msg = "не смогли скачать reel media из R2"
        fail_job(db, job, msg)
        mark_analysis_failed(db, reel, msg)
        return True

    transcript: Optional[str] = None
    visual_summary: Optional[str] = None
    scenes_json: Optional[str] = None
    hook_type: Optional[str] = None
    errors: list[str] = []

    # 1) Whisper
    try:
        transcript = transcribe_audio(media_path, user.openai_api_key)
    except TranscribeError as e:
        errors.append(f"transcribe: {e}")
        logger.warning(f"reel #{reel.id} transcribe FAILED: {e}")

    # 2) Vision summary
    try:
        visual_summary = summarize_video(media_path, user.openai_api_key)
    except VisionError as e:
        errors.append(f"vision: {e}")
        logger.warning(f"reel #{reel.id} vision FAILED: {e}")

    # 3) Scenes (ffmpeg, не нужен API key)
    try:
        scenes_list = detect_scenes(media_path)
        scenes_json = json.dumps(scenes_list)
    except SceneError as e:
        errors.append(f"scenes: {e}")
        logger.warning(f"reel #{reel.id} scenes FAILED: {e}")

    # 4) Hook classification (использует transcript + visual_summary)
    if transcript or visual_summary:
        try:
            hook_type = classify_hook(transcript, visual_summary, user.openai_api_key)
        except ClassifyError as e:
            errors.append(f"classify: {e}")
            logger.warning(f"reel #{reel.id} classify FAILED: {e}")

    _cleanup(media_path)

    # Сохраняем что смогли
    mark_analysis_done(db, reel,
                       transcript=transcript,
                       visual_summary=visual_summary,
                       scenes_json=scenes_json,
                       hook_type=hook_type)

    # Если ничего не получилось — fail. Если есть хоть что-то — complete.
    if not any([transcript, visual_summary, scenes_json, hook_type]):
        err = " | ".join(errors)[:500] or "unknown analyzer failure"
        fail_job(db, job, err)
        mark_analysis_failed(db, reel, err)
        return True

    if errors:
        # частичный успех — пишем error, но job COMPLETED
        mark_analysis_failed(db, reel, " | ".join(errors)[:500])

    complete_job(db, job, 0, 0, 0, 0)
    logger.info(
        f"✅ ANALYZE_REEL #{job.id} done: "
        f"transcript={len(transcript or '')}c, "
        f"vision={len(visual_summary or '')}c, "
        f"scenes={len(json.loads(scenes_json)) if scenes_json else 0}, "
        f"hook={hook_type}"
    )
    # PR #10 — chain hook: после analyze → recipe + remake (if enabled)
    try:
        from app.services.auto_pipeline_service import on_reel_analyzed
        on_reel_analyzed(db, reel)
    except Exception as e:
        logger.warning(f"on_reel_analyzed hook failed: {e}")
    return True
