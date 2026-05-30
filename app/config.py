"""
Конфигурация приложения через переменные окружения
"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/reelstracker"

    # JWT
    SECRET_KEY: str = "dev-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # App
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    DEBUG: bool = True

    # Proxy
    PROXY_ENABLED: bool = False
    PROXY_LIST: str = ""

    # Selenium
    CHROME_BINARY_PATH: str = ""
    CHROMEDRIVER_PATH: str = ""

    # Tariff limits
    FREE_MAX_REELS: int = 3
    FREE_PARSE_INTERVAL_MINUTES: float = 0.33  # ~20 секунд для тестирования
    PRO_PARSE_INTERVAL_MINUTES: float = 0.33

    # Cloudflare R2 storage (для GeneratedVideo media) — generation MVP.
    # Если R2_BUCKET пустой — generation_worker откажется обрабатывать
    # GENERATE_VIDEO задачи, остальная функциональность не затронута.
    R2_ACCOUNT_ID: str = ""
    R2_BUCKET: str = ""
    R2_ENDPOINT: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    # Public URL prefix — если bucket подключён к Cloudflare CDN на собственном
    # домене, ставим сюда (https://cdn.mimic.io/). Иначе worker генерит presigned.
    R2_PUBLIC_BASE_URL: str = ""
    R2_PRESIGNED_TTL_SECONDS: int = 60 * 60 * 24 * 6  # 6 дней; IG скачивает за <24ч

    # Generation MVP — Runway provider defaults.
    RUNWAY_DEFAULT_MODEL: str = "gen4.5"     # альтернатива: veo3.1_fast
    RUNWAY_DEFAULT_DURATION: int = 5         # секунд
    RUNWAY_DEFAULT_RATIO: str = "720:1280"   # 9:16 — Reels/TikTok
    RUNWAY_POLL_INTERVAL_SECONDS: int = 10
    RUNWAY_POLL_TIMEOUT_SECONDS: int = 600   # 10 минут на одно видео

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


@lru_cache()
def get_settings() -> Settings:
    return Settings()
