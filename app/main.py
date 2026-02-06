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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown events"""
    logger.info("🚀 ReelsTracker SaaS запускается...")

    # Создаём таблицы (в продакшене — alembic migrate)
    Base.metadata.create_all(bind=engine)
    logger.info("✅ Таблицы БД готовы")

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

app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(reels_router, prefix="/api/reels", tags=["Reels"])
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["Dashboard"])
app.include_router(telegram_router, prefix="/api/settings/telegram", tags=["Telegram"])
app.include_router(tariff_router, prefix="/api/tariff", tags=["Tariff"])
app.include_router(parsing_router, prefix="/api/parse", tags=["Parsing"])

# ─── Static Files ──────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    """Редирект на логин"""
    return FileResponse("static/login.html")


@app.get("/login.html")
async def login_page():
    return FileResponse("static/login.html")


@app.get("/tracker.html")
async def tracker_page():
    return FileResponse("static/tracker.html")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/debug/config")
async def debug_config():
    """Временный debug endpoint - УДАЛИТЬ после отладки!"""
    return {
        "secret_key_preview": settings.SECRET_KEY[:20] + "..." if len(settings.SECRET_KEY) > 20 else settings.SECRET_KEY,
        "secret_key_len": len(settings.SECRET_KEY),
        "algorithm": settings.ALGORITHM,
        "database_url_preview": settings.DATABASE_URL[:30] + "..." if settings.DATABASE_URL else None,
    }
