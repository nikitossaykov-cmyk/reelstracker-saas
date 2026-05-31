"""
ReelsTracker SaaS — FastAPI Application
"""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import get_settings
from app.database import engine, Base

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

    # Запуск фонового парсера и шедулера
    from app.workers.scheduler import start_scheduler_thread, start_worker_thread
    start_scheduler_thread(check_interval=30)
    start_worker_thread(poll_interval=5)
    logger.info("✅ Scheduler + Worker запущены")

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


@app.get("/forge.html")
async def forge_page():
    return FileResponse("static/forge.html")


@app.get("/forge-advanced.html")
async def forge_advanced_page():
    return FileResponse("static/forge-advanced.html")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


