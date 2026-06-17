"""
ReelsTracker SaaS — FastAPI Application
"""

import logging
import os
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import get_settings
from app.database import engine, Base
from app.core.rate_limit import RateLimitMiddleware

# Настройка логгирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

settings = get_settings()


def reattach_orphan_reels():
    """
    Спасти рилсы, которых предыдущая версия открепила (instagram_account_id=NULL),
    но которые на самом деле принадлежат аккаунту юзера: ищем по author_username
    совпадение с InstagramAccount.instagram_username и возвращаем привязку.
    Идемпотентно — безопасно вызывать каждый старт.
    """
    from app.database import SessionLocal
    from app.models.reel import Reel
    from app.models.account import InstagramAccount

    db = SessionLocal()
    try:
        orphans = db.query(Reel).filter(
            Reel.instagram_account_id.is_(None),
            Reel.author_username.isnot(None),
            Reel.platform == 'instagram',
        ).all()
        if not orphans:
            return
        # username → account, для быстрого поиска
        accs = db.query(InstagramAccount).all()
        by_user = {}
        for a in accs:
            by_user.setdefault((a.user_id, (a.instagram_username or '').lower()), a)

        reattached = 0
        for r in orphans:
            key = (r.user_id, (r.author_username or '').lower())
            acc = by_user.get(key)
            if acc:
                r.instagram_account_id = acc.id
                # position_in_account оставляем NULL — назначится при следующем sync'е
                reattached += 1
        if reattached:
            db.commit()
            logger.info(f"🔄 Восстановлена привязка {reattached} рилсов к их аккаунтам")
    except Exception as e:
        logger.warning(f"reattach_orphan_reels: {e}")
    finally:
        db.close()


def cleanup_duplicate_reels():
    """
    Одноразовая чистка дубликатных позиций: если в одном instagram_account_id
    несколько Reel имеют одинаковый position_in_account, оставляем у самого старого,
    остальным ставим NULL (попадут в конец списка). instagram_account_id НЕ трогаем,
    рилсы остаются в аккаунте.
    """
    from app.database import SessionLocal
    from app.models.reel import Reel
    from sqlalchemy import func

    db = SessionLocal()
    try:
        position_clashes = 0
        accs_with_dupes = db.query(Reel.instagram_account_id, Reel.position_in_account, func.count('*').label('cnt'))\
            .filter(Reel.instagram_account_id.isnot(None), Reel.position_in_account.isnot(None))\
            .group_by(Reel.instagram_account_id, Reel.position_in_account)\
            .having(func.count('*') > 1)\
            .all()
        for acc_id, pos, cnt in accs_with_dupes:
            dupes = db.query(Reel).filter(
                Reel.instagram_account_id == acc_id,
                Reel.position_in_account == pos,
            ).order_by(Reel.id.asc()).all()
            for r in dupes[1:]:
                r.position_in_account = None
                position_clashes += 1

        if position_clashes:
            db.commit()
            logger.info(f"🧹 Чистка дубликатных позиций: {position_clashes} записей получили NULL-позицию")
    except Exception as e:
        logger.warning(f"cleanup_duplicate_reels: {e}")
    finally:
        db.close()


def migrate_legacy_media_urls():
    """One-shot: rewrite gv.media_url / gv.uniq_media_url for rows that
    still hold a raw R2 presigned URL.  After PR #16 we store
    /api/media?key=<storage_key>, which is stable. Older rows have
    expired presigned URLs that no longer play. This converts them in
    bulk on startup. Idempotent — safe to call repeatedly.
    """
    from app.database import SessionLocal
    from app.models.generation import GeneratedVideo
    from sqlalchemy import or_

    db = SessionLocal()
    try:
        rows = (db.query(GeneratedVideo)
                .filter(or_(GeneratedVideo.media_storage_key.isnot(None),
                            GeneratedVideo.uniq_storage_key.isnot(None)))
                .all())
        n_media = 0
        n_uniq = 0
        for gv in rows:
            if gv.media_storage_key and (
                not gv.media_url or not gv.media_url.startswith("/api/media")
            ):
                gv.media_url = f"/api/media?key={gv.media_storage_key}"
                n_media += 1
            if gv.uniq_storage_key and (
                not gv.uniq_media_url or not gv.uniq_media_url.startswith("/api/media")
            ):
                gv.uniq_media_url = f"/api/media?key={gv.uniq_storage_key}"
                n_uniq += 1
        if n_media or n_uniq:
            db.commit()
            logger.info(
                f"🔁 migrate_legacy_media_urls: rewrote {n_media} media_url "
                f"+ {n_uniq} uniq_media_url to proxy form"
            )
    except Exception as e:
        logger.warning(f"migrate_legacy_media_urls skipped: {e}")
    finally:
        db.close()


def migrate_legacy_mp4_faststart():
    """Walk every READY GeneratedVideo with a media_storage_key, download
    from R2, ensure +faststart, re-upload if needed. Runs once on startup;
    skips files that are already laid out for streaming.

    Necessary because before we noticed Safari/iOS need moov-before-mdat,
    every strategy wrote files with moov at the end. With our streaming
    proxy the browser <video> couldn't read metadata and showed 0:00.
    """
    from app.database import SessionLocal
    from app.models.generation import GeneratedVideo, GenerationStatus
    from app.core.faststart import ensure_faststart, is_faststart
    import tempfile
    from pathlib import Path

    try:
        from app.core.storage import get_r2
        r2 = get_r2()
    except Exception as e:
        logger.warning(f"migrate_legacy_mp4_faststart: R2 unavailable: {e}")
        return

    db = SessionLocal()
    try:
        rows = (db.query(GeneratedVideo)
                .filter(GeneratedVideo.status == GenerationStatus.READY,
                        GeneratedVideo.media_storage_key.isnot(None))
                .all())
        rewritten = 0
        skipped = 0
        errors = 0
        workdir = Path(tempfile.mkdtemp(prefix="faststart_"))
        for gv in rows:
            key = gv.media_storage_key
            if not key.endswith(".mp4"):
                continue
            local = workdir / f"gv_{gv.id}.mp4"
            try:
                r2._client.download_file(r2.bucket, key, str(local))
            except Exception as e:
                logger.warning(f"faststart migrate gv #{gv.id} download failed: {e}")
                errors += 1
                continue
            if is_faststart(local):
                skipped += 1
                try: local.unlink(missing_ok=True)
                except OSError: pass
                continue
            try:
                changed = ensure_faststart(local)
            except Exception as e:
                logger.warning(f"faststart migrate gv #{gv.id} rewrite failed: {e}")
                errors += 1
                try: local.unlink(missing_ok=True)
                except OSError: pass
                continue
            if changed:
                try:
                    with local.open("rb") as f:
                        r2.upload_bytes(key, f.read(), content_type="video/mp4")
                    rewritten += 1
                except Exception as e:
                    logger.warning(f"faststart migrate gv #{gv.id} re-upload failed: {e}")
                    errors += 1
            try: local.unlink(missing_ok=True)
            except OSError: pass
        try: workdir.rmdir()
        except OSError: pass
        if rewritten or errors:
            logger.info(
                f"⏩ migrate_legacy_mp4_faststart: rewrote={rewritten}, "
                f"already_ok={skipped}, errors={errors}"
            )
    except Exception as e:
        logger.warning(f"migrate_legacy_mp4_faststart top-level: {e}")
    finally:
        db.close()


def reset_stuck_jobs():
    """Сброс зависших задач (RUNNING без завершения)"""
    from app.database import SessionLocal
    from app.models.parsing import ParseJob, JobStatus
    from datetime import datetime, timedelta

    db = SessionLocal()
    try:
        # Задачи в статусе RUNNING более 10 минут — считаем зависшими
        cutoff = datetime.utcnow() - timedelta(minutes=10)
        stuck_jobs = db.query(ParseJob).filter(
            ParseJob.status == JobStatus.RUNNING,
            ParseJob.started_at < cutoff
        ).all()

        for job in stuck_jobs:
            job.status = JobStatus.PENDING
            job.started_at = None
            logger.warning(f"🔄 Сброшена зависшая задача #{job.id}")

        if stuck_jobs:
            db.commit()
            logger.info(f"✅ Сброшено {len(stuck_jobs)} зависших задач")
    except Exception as e:
        logger.error(f"Ошибка сброса задач: {e}")
    finally:
        db.close()


def run_lightweight_migrations():
    """Idempotent миграции — добавляем новые колонки к существующим таблицам.
    Альтернатива alembic для мелких изменений, безопасно на уже-живой БД."""
    from sqlalchemy import text
    migrations = [
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS thumbnail_url VARCHAR(1024)",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS author_username VARCHAR(255)",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS author_full_name VARCHAR(255)",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS published_at TIMESTAMP",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS caption TEXT",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS duration_seconds DOUBLE PRECISION",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS instagram_account_id INTEGER",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS position_in_account INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_reels_instagram_account_id ON reels(instagram_account_id)",
        # parse_jobs: sync-задачи не привязаны к одному рилсу
        "ALTER TABLE parse_jobs ALTER COLUMN reel_id DROP NOT NULL",
        "ALTER TABLE parse_jobs ADD COLUMN IF NOT EXISTS account_id INTEGER",
        "DO $$ BEGIN CREATE TYPE jobtype AS ENUM ('PARSE_REEL','SYNC_ACCOUNT'); EXCEPTION WHEN duplicate_object THEN NULL; END $$",
        "ALTER TABLE parse_jobs ADD COLUMN IF NOT EXISTS job_type jobtype",
        "UPDATE parse_jobs SET job_type = 'PARSE_REEL' WHERE job_type IS NULL",
        # users: Apify token
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS apify_token VARCHAR(255)",
        # users: кредитный баланс + per-user provider ключи (для generation MVP)
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS credits_balance_kopecks BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS runway_api_key VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS elevenlabs_api_key VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS openai_api_key VARCHAR(255)",
        # parse_jobs: FK на новые сущности из generation MVP
        "ALTER TABLE parse_jobs ADD COLUMN IF NOT EXISTS generated_video_id INTEGER",
        "ALTER TABLE parse_jobs ADD COLUMN IF NOT EXISTS post_id INTEGER",
        "ALTER TABLE parse_jobs ADD COLUMN IF NOT EXISTS posting_target_id INTEGER",
        # Content Forge — медиа-кэш рилсов в R2 для ремейка/анализа.
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS media_storage_key VARCHAR(512)",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS media_size_bytes BIGINT",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS media_downloaded_at TIMESTAMP",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS media_source_url TEXT",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS media_download_error TEXT",
        "CREATE INDEX IF NOT EXISTS ix_reels_media_downloaded "
        "ON reels(media_downloaded_at) WHERE media_storage_key IS NOT NULL",
        # InstagramAccount — флаг auto-download для target-аккаунтов.
        "ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS "
        "auto_download_media BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS "
        "auto_analyze_media BOOLEAN NOT NULL DEFAULT FALSE",
        # Content Forge analyzer — результаты Whisper/Vision/scenes.
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS transcript TEXT",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS visual_summary TEXT",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS scenes TEXT",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS hook_type VARCHAR(64)",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS analyzed_at TIMESTAMP",
        "ALTER TABLE reels ADD COLUMN IF NOT EXISTS analysis_error TEXT",
        # PR #5 — content_recipes таблица создаётся через create_all,
        # тут ничего дополнительно делать не нужно (нет ALTER колонок).
        # PR #6 — remake-pointers в generated_videos
        "ALTER TABLE generated_videos ADD COLUMN IF NOT EXISTS source_reel_id INTEGER",
        "ALTER TABLE generated_videos ADD COLUMN IF NOT EXISTS source_recipe_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_generated_videos_source_reel "
        "ON generated_videos(source_reel_id)",
        "CREATE INDEX IF NOT EXISTS ix_generated_videos_source_recipe "
        "ON generated_videos(source_recipe_id)",
        # PR #8 — uniq media копия
        "ALTER TABLE generated_videos ADD COLUMN IF NOT EXISTS uniq_media_url VARCHAR(1024)",
        "ALTER TABLE generated_videos ADD COLUMN IF NOT EXISTS uniq_storage_key VARCHAR(512)",
        "ALTER TABLE generated_videos ADD COLUMN IF NOT EXISTS uniqified_at TIMESTAMP",
        # PR #10 — auto-remake pipeline
        "ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS auto_remake_enabled BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS auto_uniqify BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS auto_publish BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS viral_growth_threshold DOUBLE PRECISION NOT NULL DEFAULT 2.0",
        "ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS viral_window_hours INTEGER NOT NULL DEFAULT 12",
        "ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS default_remake_params JSONB",
        "ALTER TABLE instagram_accounts ADD COLUMN IF NOT EXISTS auto_posting_target_id INTEGER",
        # Strategy E — per-user Replicate token + persona link + mode + per-call costs.
        # Personas table itself is created via Base.metadata.create_all.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS replicate_api_key VARCHAR(255)",
        "ALTER TABLE generated_videos ADD COLUMN IF NOT EXISTS persona_id INTEGER",
        "ALTER TABLE generated_videos ADD COLUMN IF NOT EXISTS mode SMALLINT",
        "ALTER TABLE generated_videos ADD COLUMN IF NOT EXISTS cost_runpod_seconds DOUBLE PRECISION",
        "ALTER TABLE generated_videos ADD COLUMN IF NOT EXISTS cost_replicate_usd NUMERIC(10,4)",
        "CREATE INDEX IF NOT EXISTS ix_generated_videos_persona_id "
        "ON generated_videos(persona_id)",
    ]
    # Enum-расширения нужно делать в AUTOCOMMIT (Postgres не разрешает
    # ALTER TYPE ... ADD VALUE внутри транзакции).
    enum_extensions = [
        "ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'GENERATE_VIDEO'",
        "ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'POST_TO_INSTAGRAM'",
        "ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'POST_TO_TIKTOK'",
        "ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'POST_TO_VK'",
        "ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'POST_TO_YOUTUBE'",
        "ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'OAUTH_REFRESH'",
        "ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'ANALYZE_REEL'",
        "ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'REMAKE_VIDEO'",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception as e:
                logger.warning(f"Миграция '{sql[:60]}...' не прошла: {e}")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for sql in enum_extensions:
            try:
                conn.execute(text(sql))
            except Exception as e:
                logger.warning(f"Enum-расширение '{sql[:60]}...' не прошло: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown events"""
    logger.info("🚀 ReelsTracker SaaS запускается...")

    # Создаём таблицы (в продакшене — alembic migrate)
    Base.metadata.create_all(bind=engine)
    logger.info("✅ Таблицы БД готовы")

    # Лёгкие миграции для новых колонок
    run_lightweight_migrations()
    logger.info("✅ Миграции колонок выполнены")

    # Сброс зависших задач от предыдущего запуска
    reset_stuck_jobs()

    # Спасаем рилсы, которых предыдущая версия открепила
    reattach_orphan_reels()

    # Одноразовая чистка дубликатных позиций (instagram_account_id не трогаем)
    cleanup_duplicate_reels()

    # Convert any legacy raw R2 media_urls to the stable /api/media proxy
    # form. Idempotent — rows already on proxy form are skipped.
    migrate_legacy_media_urls()

    # Rewrite any READY mp4 in R2 so moov precedes mdat (browser streaming).
    # Idempotent and runs as a background thread so it never blocks startup.
    import threading
    threading.Thread(
        target=migrate_legacy_mp4_faststart,
        name="faststart-migrate", daemon=True,
    ).start()

    # Idempotent: encrypt any plaintext OAuth tokens left from pre-Fernet rows.
    # Silently skipped if OAUTH_TOKEN_FERNET_KEY not set (first deploy).
    try:
        from app.database import SessionLocal
        from app.services.posting_target_service import migrate_legacy_plaintext_tokens
        with SessionLocal() as _db:
            migrate_legacy_plaintext_tokens(_db)
    except Exception as e:
        logger.warning(f"OAuth-token re-encryption skipped: {e}")

    # Запуск фонового парсера и шедулера
    from app.workers.scheduler import start_scheduler_thread, start_worker_thread
    start_scheduler_thread(check_interval=30)
    start_worker_thread(poll_interval=5)
    logger.info("✅ Scheduler + Worker запущены")

    # Persona library worker — drains 'personas' rows in GENERATING
    # via Replicate PuLID-Flux. Mandatory for Strategy E: without it
    # the create-persona flow hangs forever at "генерация 4 кандидатов".
    if os.getenv("WORKER_PERSONA", "1") == "1":
        from app.database import SessionLocal as _PersonaSession
        from app.workers.persona_worker import run_loop as persona_loop
        threading.Thread(
            target=persona_loop, args=(_PersonaSession,),
            daemon=True, name="persona-worker",
        ).start()
        logger.info("✅ Persona worker запущен")

    # Strategy E (face replace) worker — drains generated_videos rows
    # with mode IS NOT NULL. MVP placement: daemon thread in the web
    # process, matching the existing parser_worker pattern. Tracked in
    # Forge tech insights for extraction to a dedicated Railway service.
    if os.getenv("WORKER_FORGE_E", "1") == "1":
        from app.database import SessionLocal
        from app.workers.forge_e_worker import (
            run_loop as forge_e_loop,
            sweep_stuck_running as forge_e_sweep,
        )
        try:
            forge_e_sweep(SessionLocal, max_minutes=30)
        except Exception as e:
            logger.warning(f"forge_e sweep failed: {e}")
        threading.Thread(
            target=forge_e_loop, args=(SessionLocal,),
            daemon=True, name="forge-e-worker",
        ).start()
        logger.info("✅ Forge E worker запущен")

    # MakeUGC worker — drains makeugc_jobs rows through the UGC reel
    # pipeline (Strategy MakeUGC). Scaffold scope: PENDING → PORTRAIT →
    # READY only; voiceover / lipsync / cutaway / concat land in
    # follow-up PRs.
    if os.getenv("WORKER_MAKEUGC", "1") == "1":
        from app.database import SessionLocal as _MakeUGCSession
        from app.workers.makeugc_worker import run_loop as makeugc_loop
        threading.Thread(
            target=makeugc_loop, args=(_MakeUGCSession,),
            daemon=True, name="makeugc-worker",
        ).start()
        logger.info("✅ MakeUGC worker запущен")

    # RunPod auto-stop tick — every minute, stops the Wan pod if no
    # Mode 2 row has been RUNNING within WAN_POD_IDLE_MIN minutes.
    if os.getenv("WAN_POD_AUTOSTOP", "1") == "1":
        from app.services.runpod_pod import stop_pod_if_idle

        def _wan_pod_autostop_tick():
            import time as _time
            idle_min = int(os.getenv("WAN_POD_IDLE_MIN", "10"))
            while True:
                try:
                    stop_pod_if_idle(max_idle_minutes=idle_min)
                except Exception:
                    logger.exception("wan pod autostop tick failed")
                _time.sleep(60)

        threading.Thread(
            target=_wan_pod_autostop_tick,
            daemon=True, name="wan-pod-autostop",
        ).start()
        logger.info("✅ Wan pod autostop включён")

    yield

    logger.info("👋 ReelsTracker SaaS остановлен")


app = FastAPI(
    title="ReelsTracker SaaS",
    description="Трекер метрик рилсов с мульти-юзерной поддержкой",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting (per-IP, in-memory). See app/core/rate_limit.py for rules.
app.add_middleware(RateLimitMiddleware)

# ─── API Routes ────────────────────────────────────────────

from app.api.auth import router as auth_router
from app.api.reels import router as reels_router
from app.api.dashboard import router as dashboard_router
from app.api.telegram import router as telegram_router
from app.api.tariff import router as tariff_router
from app.api.parsing import router as parsing_router
from app.api.accounts import router as accounts_router
from app.api.settings_apify import router as apify_router
from app.api.generation import router as generation_router
from app.api.recipes import router as recipes_router
from app.api.remakes import router as remakes_router
from app.api.posting_targets import router as posting_targets_router
from app.api.posts import router as posts_router
from app.api.voice import router as voice_router
from app.api.magic import router as magic_router
from app.api.account_insights import router as account_insights_router
from app.api.forge import router as forge_router
from app.api.media import router as media_router
from app.api.personas import router as personas_router
from app.api.makeugc import router as makeugc_router

app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(reels_router, prefix="/api/reels", tags=["Reels"])
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["Dashboard"])
app.include_router(telegram_router, prefix="/api/settings/telegram", tags=["Telegram"])
app.include_router(tariff_router, prefix="/api/tariff", tags=["Tariff"])
app.include_router(parsing_router, prefix="/api/parse", tags=["Parsing"])
app.include_router(accounts_router, prefix="/api/accounts", tags=["Accounts"])
app.include_router(apify_router, prefix="/api/settings/apify", tags=["Apify"])
app.include_router(generation_router, prefix="/api/generation", tags=["Generation"])
app.include_router(recipes_router, prefix="/api/recipes", tags=["Recipes"])
app.include_router(remakes_router, prefix="/api/remakes", tags=["Remakes"])
app.include_router(posting_targets_router, prefix="/api/posting-targets", tags=["PostingTargets"])
app.include_router(posts_router, prefix="/api/posts", tags=["Posts"])
app.include_router(voice_router, prefix="/api/voice", tags=["Voice"])
app.include_router(magic_router, prefix="/api/magic", tags=["Magic"])
app.include_router(account_insights_router, prefix="/api/account-insights", tags=["AccountInsights"])
app.include_router(forge_router, prefix="/api/forge", tags=["Forge"])
app.include_router(media_router, prefix="/api/media", tags=["Media"])
app.include_router(personas_router, prefix="/api/personas", tags=["Personas"])
app.include_router(makeugc_router, prefix="/api/makeugc", tags=["MakeUGC"])

# ─── Static Files ──────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    """Главная страница — трекер"""
    return FileResponse("static/tracker.html")


@app.get("/login.html")
async def login_page():
    return FileResponse("static/login.html")


@app.get("/tracker.html")
async def tracker_page():
    return FileResponse("static/tracker.html")


@app.get("/forge")
async def forge_page_redirect():
    return FileResponse("static/forge.html")


@app.get("/settings")
async def settings_page_redirect():
    return FileResponse("static/settings.html")


@app.get("/personas")
async def personas_page():
    return FileResponse("static/personas.html")


@app.get("/personas.html")
async def personas_page_html():
    return FileResponse("static/personas.html")


@app.get("/settings.html")
async def settings_page():
    return FileResponse("static/settings.html")


@app.get("/forge.html")
async def forge_page():
    return FileResponse("static/forge.html")


@app.get("/forge-advanced.html")
async def forge_advanced_page():
    return FileResponse("static/forge-advanced.html")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


