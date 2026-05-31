"""
Magic Mode orchestrator — один-click пайплайн «дай URL/файл → получи ремейк».

Принимает либо URL чужого виральнего рилса, либо загруженный MP4.
Делает: download → R2 cache → ANALYZE_REEL → on_reel_analyzed hook
автоматически extract recipe + create remake → generation worker
делает Runway gen → uniqify (опц) → результат.

Прогресс трекается через стандартные models (Reel.analyzed_at,
GeneratedVideo.status) — UI запрашивает /api/magic/{magic_job_id}/status.
"""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.reel import Reel
from app.models.parsing import ParseJob, JobStatus, JobType
from app.models.generation import GeneratedVideo, GenerationStatus, VideoProvider
from app.services.tariff_service import get_priority
from app.core.composer import RemakeParams
from app.core.media_service_helpers import upload_to_r2

logger = logging.getLogger(__name__)


def _upload_local_to_r2(local: Path, user_id: int) -> tuple[str, int]:
    from app.core.storage import get_r2
    r2 = get_r2()
    key = f"users/{user_id}/magic_source/{uuid.uuid4().hex[:12]}.mp4"
    with local.open("rb") as f:
        data = f.read()
    r2.upload_bytes(key, data, content_type="video/mp4")
    return key, len(data)


def _extract_first_frame_and_upload(local_mp4: Path, user_id: int) -> Optional[str]:
    """Извлечь первый кадр через ffmpeg → залить в R2 → вернуть public URL.

    Используется как init_image для Runway image_to_video — это даёт
    максимальное визуальное соответствие source-видео с самого старта
    генерации. Без этого Runway фантазирует с нуля.
    """
    import subprocess
    workdir = Path(tempfile.mkdtemp(prefix=f"frame_{user_id}_"))
    out_jpg = workdir / "frame0.jpg"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-ss", "0.5", "-i", str(local_mp4),
             "-frames:v", "1", "-q:v", "2",
             "-vf", "scale=720:-2", str(out_jpg)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if r.returncode != 0 or not out_jpg.exists():
            logger.warning(f"ffmpeg first-frame failed: {r.stderr[:200]}")
            return None
        from app.core.storage import get_r2
        r2 = get_r2()
        key = f"users/{user_id}/magic_init/{uuid.uuid4().hex[:12]}.jpg"
        with out_jpg.open("rb") as f:
            r2.upload_bytes(key, f.read(), content_type="image/jpeg")
        return r2.get_public_url(key)
    finally:
        try: out_jpg.unlink(missing_ok=True); workdir.rmdir()
        except OSError: pass


def start_magic_from_url(
    db: Session,
    user: User,
    source_url: str,
    *,
    face_image_url: Optional[str] = None,
    brand: Optional[str] = None,
    product_description: Optional[str] = None,
    voice_ref_url: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    provider: VideoProvider = VideoProvider.RUNWAY,
    model: Optional[str] = None,
    duration_seconds: int = 5,
) -> dict:
    """Полный pipeline по URL чужого видео.

    Шаги:
      1. yt-dlp download
      2. R2 upload
      3. Create Reel record with media_storage_key
      4. Enqueue ANALYZE_REEL — auto_pipeline_service.on_reel_analyzed
         автоматически extract recipe + create remake (если account
         auto_remake_enabled… но для magic'а account=NULL, поэтому
         нужно явно создать remake после analyze)

    Чтобы не зависеть от auto_pipeline (нужен InstagramAccount), magic
    создаёт **placeholder InstagramAccount** для каждой Magic-job, с
    auto_remake_enabled=True и заполненным default_remake_params из
    переданных юзером параметров. После завершения — account
    удаляется (или остаётся для аудита).

    На самом деле проще — не плодить placeholder. Magic просто:
      - создаёт Reel (без instagram_account_id) с media в R2
      - ставит ANALYZE_REEL
      - **возвращает reel_id**; UI поллит /api/magic/{reel_id}/status
      - когда analyze done → UI дёргает /api/recipes/from-reel/{id}
        потом /api/remakes/from-recipe (всё уже есть)

    Но это два хопа. Лучше внутри magic'а:
      - upload + создать Reel + ANALYZE
      - возвращать reel_id; UI поллит
      - когда analyze done — magic_orchestrator (отдельный воркер)
        авто-extract + auto-remake

    Самое простое: создать MagicJob сущность... не хочу плодить.

    Решение: используем уже-существующий механизм — создаём
    placeholder InstagramAccount с auto_remake_enabled и
    default_remake_params для этого юзера; новый Reel привязывается
    к нему. on_reel_analyzed hook автоматически extract+remake.
    """
    from app.core.yt_downloader import download_video, DownloadError
    from app.services.media_service import download_reel_media  # noqa
    from app.services.analysis_service import create_analyze_job
    from app.models.account import InstagramAccount

    # 1. yt-dlp
    try:
        local_path, meta = download_video(source_url)
    except DownloadError as e:
        raise ValueError(f"download failed: {e}")

    # 2. R2 upload + extract first frame for image-to-video seed
    init_image_url: Optional[str] = None
    try:
        key, size = _upload_local_to_r2(local_path, user.id)
        try:
            init_image_url = _extract_first_frame_and_upload(local_path, user.id)
            if init_image_url:
                logger.info(f"🪄 Magic: extracted first frame as init_image")
        except Exception as e:
            logger.warning(f"first-frame extraction failed (will fallback to text2video): {e}")
    finally:
        try: local_path.unlink(missing_ok=True); local_path.parent.rmdir()
        except OSError: pass

    # 3. Placeholder InstagramAccount для auto_pipeline trigger
    #    (один на юзера, переиспользуется между magic-job-ами)
    magic_acc = (db.query(InstagramAccount)
                 .filter(InstagramAccount.user_id == user.id,
                         InstagramAccount.instagram_username == "__magic_mode__")
                 .first())
    # Match remake duration to source (Runway gen4.5 max 10s).
    src_dur = meta.get("duration") or duration_seconds
    matched_duration = min(max(int(src_dur), 5), 10)
    remake_params = {
        k: v for k, v in {
            "brand": brand,
            "product_description": product_description,
            "extra_instructions": extra_instructions,
            "duration_seconds": matched_duration,
            "model": model,
            "init_image_url": init_image_url,  # PR #20: image-to-video seed
        }.items() if v is not None
    }
    if not magic_acc:
        magic_acc = InstagramAccount(
            user_id=user.id,
            instagram_username="__magic_mode__",
            full_name="Magic Mode (auto-pipeline trigger)",
            sync_enabled=False,
            auto_download_media=False,
            auto_analyze_media=True,
            auto_remake_enabled=True,
            auto_uniqify=True,
            auto_publish=False,
            viral_growth_threshold=999.0,  # не триггерится на metrics
            viral_window_hours=12,
            default_remake_params=remake_params,
        )
        db.add(magic_acc); db.commit(); db.refresh(magic_acc)
    else:
        # Обновим params свежими
        magic_acc.default_remake_params = remake_params
        magic_acc.auto_analyze_media = True
        magic_acc.auto_remake_enabled = True
        db.commit()

    # 4. Create Reel
    reel = Reel(
        user_id=user.id,
        instagram_account_id=magic_acc.id,
        title=(meta.get("title") or f"Magic from {meta.get('platform')}")[:255],
        platform=meta.get("platform") or "instagram",
        url=meta.get("webpage_url") or source_url,
        enabled=False,  # не парсим заново
        views=meta.get("view_count") or 0,
        likes=meta.get("like_count") or 0,
        comments=meta.get("comment_count") or 0,
        shares=0,
        author_username=meta.get("uploader"),
        author_full_name=meta.get("uploader_id"),
        thumbnail_url=meta.get("thumbnail"),
        caption=meta.get("description"),
        duration_seconds=meta.get("duration"),
        media_storage_key=key,
        media_size_bytes=size,
        media_downloaded_at=datetime.utcnow(),
        media_source_url=source_url,
    )
    db.add(reel); db.commit(); db.refresh(reel)
    logger.info(f"🪄 Magic: created reel #{reel.id} from {source_url[:60]}")

    # 5. Enqueue ANALYZE_REEL — on_reel_analyzed hook сделает chain
    create_analyze_job(db, user, reel)

    return {
        "reel_id": reel.id,
        "magic_account_id": magic_acc.id,
        "source_meta": meta,
        "next_step": "polling /api/magic/{reel_id}/status",
    }


def magic_status(db: Session, user: User, reel_id: int) -> dict:
    """Получить прогресс magic-job по reel_id.

    Стадии:
      1. uploaded — Reel создан, media в R2
      2. analyzing — ANALYZE_REEL в очереди или RUNNING
      3. analyzed — transcript/vision/scenes есть → ищем recipe
      4. recipe_ready — ContentRecipe есть → ищем remake
      5. generating — GeneratedVideo есть, RUNNING
      6. ready — GeneratedVideo READY с media_url
      7. failed — ошибка где-то
    """
    from app.models.recipe import ContentRecipe
    reel = (db.query(Reel)
            .filter(Reel.id == reel_id, Reel.user_id == user.id)
            .first())
    if not reel:
        return {"status": "not_found"}

    stage = "uploaded"
    detail = {}

    # Check analyze job
    last_an = (db.query(ParseJob)
               .filter(ParseJob.reel_id == reel.id,
                       ParseJob.job_type == JobType.ANALYZE_REEL)
               .order_by(ParseJob.id.desc()).first())
    if last_an:
        stage = "analyzing"
        detail["analyze_job_status"] = last_an.status.value
        if last_an.status == JobStatus.FAILED:
            return {"reel_id": reel.id, "status": "failed", "where": "analyze",
                    "error": last_an.error_message, "detail": detail}

    if reel.analyzed_at:
        stage = "analyzed"
        detail["hook_type"] = reel.hook_type
        detail["transcript_chars"] = len(reel.transcript or "")
        detail["visual_summary_chars"] = len(reel.visual_summary or "")

    # Check recipe
    recipe = (db.query(ContentRecipe)
              .filter(ContentRecipe.source_reel_id == reel.id)
              .order_by(ContentRecipe.created_at.desc()).first())
    if recipe:
        stage = "recipe_ready"
        detail["recipe_id"] = recipe.id
        detail["recipe_name"] = recipe.name

    # Check generation
    gv = (db.query(GeneratedVideo)
          .filter(GeneratedVideo.source_reel_id == reel.id)
          .order_by(GeneratedVideo.created_at.desc()).first())
    if gv:
        stage = "generating"
        detail["generation_id"] = gv.id
        detail["generation_status"] = gv.status.value
        if gv.status == GenerationStatus.READY:
            stage = "ready"
            detail["media_url"] = gv.media_url
            detail["uniq_media_url"] = gv.uniq_media_url
        elif gv.status == GenerationStatus.FAILED:
            return {"reel_id": reel.id, "status": "failed", "where": "generation",
                    "error": gv.error_message, "detail": detail}

    return {
        "reel_id": reel.id,
        "status": stage,
        "source_url": reel.media_source_url,
        "source_title": reel.title,
        "detail": detail,
    }
