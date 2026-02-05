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
    FREE_PARSE_INTERVAL_MINUTES: int = 60
    PRO_PARSE_INTERVAL_MINUTES: int = 5

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


@lru_cache()
def get_settings() -> Settings:
    return Settings()
