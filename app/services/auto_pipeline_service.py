"""
Auto-Remake Pipeline (PR #10).

Связывает все стадии Content Forge в один автоматический chain через
hooks из workers:

    on_reel_metrics_updated(reel)       — после parse_reel_job → детект
                                          virality → enqueue chain
    on_reel_downloaded(reel)            — после download → enqueue analyze
    on_reel_analyzed(reel)              — после analyze → extract recipe
                                          + create remake job
    on_generation_ready(gv)             — после Runway → uniqify (if
                                          enabled) → create post → publish
                                          (if auto_publish)

Все hooks безопасны: если что-то не настроено (auto_remake_enabled=False,
нет API key, нет posting_target и т.д.) — просто молчат, не падают.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.reel import Reel, ReelHistory
from app.models.account import InstagramAccount
from app.models.generation import GeneratedVideo, GenerationStatus, VideoProvider
from app.models.recipe import ContentRecipe

logger = logging.getLogger(__name__)


# ============================================================================
# Хук 1: после обновления metrics конкурентного рилса — детект виральности
# ============================================================================

def detect_and_trigger_viral(db: Session, reel: Reel) -> Optional[str]:
    """Проверить growth по последней ReelHistory; если threshold пройден
    и аккаунт настроен на auto_remake → начать chain.

    Возвращает строку с описанием решения (или None если ничего не делаем).
    """
    if not reel.instagram_account_id:
        return None
    acc = db.query(InstagramAccount).filter(
        InstagramAccount.id == reel.instagram_account_id
    ).first()
    if not acc or not acc.auto_remake_enabled:
        return None

    # Защита от двойного триггера: если этот reel уже remake-нут
    # пользователем — пропускаем.
    existing = db.query(GeneratedVideo).filter(
        GeneratedVideo.source_reel_id == reel.id,
        GeneratedVideo.status.in_([
            GenerationStatus.PENDING, GenerationStatus.RUNNING,
            GenerationStatus.UPLOADING, GenerationStatus.READY,
        ]),
    ).first()
    if existing:
        return None  # уже занимаемся / готово

    # Growth detection: сравниваем последний snapshot с тем, что был
    # window_hours назад.
    cutoff = datetime.utcnow() - timedelta(hours=acc.viral_window_hours)
    older = (db.query(ReelHistory)
             .filter(ReelHistory.reel_id == reel.id,
                     ReelHistory.parsed_at <= cutoff)
             .order_by(ReelHistory.parsed_at.desc()).first())
    if not older or not older.views:
        return None  # нет базы для сравнения
    current_views = reel.views or 0
    if current_views < 1000:
        return None  # слишком мало, чтобы говорить о виральности
    ratio = current_views / max(older.views, 1)
    if ratio < acc.viral_growth_threshold:
        return None

    logger.info(
        f"🔥 VIRAL DETECT reel #{reel.id} @{acc.instagram_username}: "
        f"{older.views} → {current_views} views (x{ratio:.1f} in "
        f"{acc.viral_window_hours}h, threshold x{acc.viral_growth_threshold})"
    )

    # Trigger chain: первый шаг зависит от того что уже сделано
    return _trigger_next_step(db, reel, acc, "viral_alert")


# ============================================================================
# Хуки из workers (после успешного завершения каждой стадии)
# ============================================================================

def on_reel_downloaded(db: Session, reel: Reel) -> None:
    """После download → если auto_analyze (или auto_remake) — enqueue analyze."""
    if not reel.instagram_account_id:
        return
    acc = db.query(InstagramAccount).filter(
        InstagramAccount.id == reel.instagram_account_id
    ).first()
    if not acc:
        return
    if not (acc.auto_analyze_media or acc.auto_remake_enabled):
        return
    _enqueue_analyze(db, reel)


def on_reel_analyzed(db: Session, reel: Reel) -> None:
    """После analyze → если auto_remake — extract recipe + create remake."""
    if not reel.instagram_account_id:
        return
    acc = db.query(InstagramAccount).filter(
        InstagramAccount.id == reel.instagram_account_id
    ).first()
    if not acc or not acc.auto_remake_enabled:
        return
    user = db.query(User).filter(User.id == reel.user_id).first()
    if not user:
        return
    _extract_recipe_and_remake(db, user, acc, reel)


def on_generation_ready(db: Session, gv: GeneratedVideo) -> None:
    """После Runway → uniqify (если auto_uniqify) → create+publish post
    (если auto_publish)."""
    if not gv.source_reel_id:
        return  # не remake, ручная генерация — chain не запускаем
    reel = db.query(Reel).filter(Reel.id == gv.source_reel_id).first()
    if not reel or not reel.instagram_account_id:
        return
    acc = db.query(InstagramAccount).filter(
        InstagramAccount.id == reel.instagram_account_id
    ).first()
    if not acc:
        return
    user = db.query(User).filter(User.id == gv.user_id).first()
    if not user:
        return

    if acc.auto_uniqify and not gv.uniq_storage_key:
        try:
            from app.services.uniqify_service import uniqify_generated_video
            uniqify_generated_video(db, gv)
            logger.info(f"auto-uniqified gv #{gv.id}")
        except Exception as e:
            logger.warning(f"auto-uniqify failed for gv #{gv.id}: {e}")
            # continue — publish даже если uniq упал (с предупреждением)

    if acc.auto_publish and acc.auto_posting_target_id:
        try:
            _auto_publish(db, user, gv, acc)
        except Exception as e:
            logger.warning(f"auto-publish failed for gv #{gv.id}: {e}")


# ============================================================================
# Внутренние helpers
# ============================================================================

def _trigger_next_step(
    db: Session, reel: Reel, acc: InstagramAccount, source: str,
) -> str:
    """Решить, с какого шага начать chain для этого reel."""
    if not reel.media_storage_key:
        # Нет медиа — нужно сначала скачать. Но download происходит в
        # _process_sync_account_job в момент sync; если на момент virality
        # ещё не было — ставим явный download-trigger через manual API
        # внутри chain. Минимум: лог-предупреждение, попросим юзера
        # включить auto_download_media.
        logger.warning(
            f"chain trigger from {source} for reel #{reel.id} but no media — "
            f"enable auto_download_media on account #{acc.id}"
        )
        return "skipped_no_media"
    if not (reel.transcript or reel.visual_summary):
        _enqueue_analyze(db, reel)
        return "analyze_enqueued"
    # Уже analyzed — extract recipe + remake
    user = db.query(User).filter(User.id == reel.user_id).first()
    if user:
        _extract_recipe_and_remake(db, user, acc, reel)
        return "recipe_and_remake_enqueued"
    return "no_user"


def _enqueue_analyze(db: Session, reel: Reel) -> None:
    """Поставить ANALYZE_REEL задачу (idempotent)."""
    from app.services.parsing_service import get_priority  # noqa
    from app.models.parsing import ParseJob, JobStatus, JobType
    existing = db.query(ParseJob).filter(
        ParseJob.reel_id == reel.id,
        ParseJob.job_type == JobType.ANALYZE_REEL,
        ParseJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
    ).first()
    if existing:
        return
    user = db.query(User).filter(User.id == reel.user_id).first()
    if not user or not user.openai_api_key:
        logger.warning(f"auto-analyze skipped for reel #{reel.id}: no openai key")
        return
    from app.services.analysis_service import create_analyze_job
    create_analyze_job(db, user, reel)
    logger.info(f"📋 auto-enqueued ANALYZE_REEL for reel #{reel.id}")


def _extract_recipe_and_remake(
    db: Session, user: User, acc: InstagramAccount, reel: Reel,
) -> None:
    """Sync extract recipe → enqueue remake job."""
    # Recipe — берём существующий или экстрагируем новый
    recipe = (db.query(ContentRecipe)
              .filter(ContentRecipe.source_reel_id == reel.id)
              .order_by(ContentRecipe.created_at.desc()).first())
    if not recipe:
        try:
            from app.services.recipe_service import extract_recipe_from_reel
            recipe = extract_recipe_from_reel(db, user, reel)
        except Exception as e:
            logger.warning(f"auto-extract recipe failed reel #{reel.id}: {e}")
            return

    # PR #21 — classify scene types so hybrid remake worker can decide
    # which segments to regenerate vs keep_original vs text_template.
    # Cheap (~$0.001/scene with gpt-4o-mini Vision).
    try:
        _ensure_scenes_classified(db, user, reel)
    except Exception as e:
        logger.warning(f"scene classification failed reel #{reel.id}: {e}")

    # Remake params из аккаунт-defaults
    from app.core.composer import RemakeParams
    p = acc.default_remake_params or {}
    params = RemakeParams(
        brand=p.get("brand"),
        product_description=p.get("product_description"),
        face_description=p.get("face_description"),
        voice_description=p.get("voice_description"),
        location_description=p.get("location_description"),
        outfit_description=p.get("outfit_description"),
        palette=p.get("palette"),
        extra_instructions=p.get("extra_instructions"),
        init_image_url=p.get("init_image_url"),  # PR #20: image-to-video seed
    )
    try:
        from app.services.remake_service import create_remake_job
        # PR #21 — если scenes классифицированы, использовать hybrid mode
        import json as _json
        scenes_data = []
        try:
            scenes_data = _json.loads(reel.scenes or "[]")
        except Exception:
            scenes_data = []
        use_hybrid = (
            len(scenes_data) > 1
            and all("type" in s for s in scenes_data)
        )
        gv = create_remake_job(
            db, user,
            recipe=recipe, source_reel=reel,
            params=params,
            provider=VideoProvider.RUNWAY,
            aspect_ratio="9:16",
            duration_seconds=int(p.get("duration_seconds") or 5),
            model=p.get("model"),
            use_hybrid=use_hybrid,
        )
        logger.info(
            f"🎬 auto-remake gv #{gv.id} for reel #{reel.id}"
            f" (mode={'HYBRID' if use_hybrid else 'single-shot'})"
        )
    except Exception as e:
        logger.warning(f"auto-remake create failed reel #{reel.id}: {e}")


def _ensure_scenes_classified(db: Session, user: User, reel: Reel) -> None:
    """Enrich reel.scenes JSON with per-scene type + reuse strategy
    (PR #21). Idempotent — если все scenes уже имеют 'type', no-op.
    Требует reel.media_storage_key + user.openai_api_key."""
    import json as _json
    try:
        scenes = _json.loads(reel.scenes or "[]")
    except Exception:
        return
    if not scenes or all("type" in s for s in scenes):
        return
    if not (reel.media_storage_key and user.openai_api_key):
        return

    import tempfile
    from pathlib import Path
    from app.core.storage import get_r2
    from app.core.scene_classifier import classify_scenes
    workdir = Path(tempfile.mkdtemp(prefix=f"sc_{reel.id}_"))
    src = workdir / "src.mp4"
    try:
        r2 = get_r2()
        r2._client.download_file(r2.bucket, reel.media_storage_key, str(src))
        enriched = classify_scenes(src, scenes, user.openai_api_key)
        reel.scenes = _json.dumps(enriched)
        db.commit()
        logger.info(f"📐 classified {len(enriched)} scenes for reel #{reel.id}")
    finally:
        try: src.unlink(missing_ok=True); workdir.rmdir()
        except OSError: pass


def _auto_publish(
    db: Session, user: User, gv: GeneratedVideo, acc: InstagramAccount,
) -> None:
    from app.services.posting_target_service import get_target_by_id
    target = get_target_by_id(db, acc.auto_posting_target_id, user)
    if not target:
        logger.warning(f"auto-publish: posting_target #{acc.auto_posting_target_id} not found")
        return
    # Caption — из default_remake_params или template
    p = acc.default_remake_params or {}
    caption = p.get("caption") or target.default_caption_template or ""
    from app.services.posting_service import create_post
    post = create_post(db, user,
                       generated_video=gv, posting_target=target,
                       caption=caption, publish_now=True)
    logger.info(f"📤 auto-publish post #{post.id} (gv #{gv.id} → target #{target.id})")
