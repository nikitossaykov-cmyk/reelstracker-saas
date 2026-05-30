"""
Модель пользователя
"""

import enum
from datetime import datetime
from sqlalchemy import Column, Integer, BigInteger, String, Boolean, DateTime, Enum, Float
from sqlalchemy.orm import relationship
from app.database import Base


class TariffType(str, enum.Enum):
    FREE = "free"
    PRO = "pro"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    tariff = Column(Enum(TariffType), default=TariffType.FREE, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Telegram settings (per-user)
    telegram_enabled = Column(Boolean, default=False)
    telegram_bot_token = Column(String(255), nullable=True)
    telegram_chat_id = Column(String(100), nullable=True)
    telegram_notify_start = Column(Boolean, default=True)
    telegram_notify_complete = Column(Boolean, default=True)
    telegram_notify_viral = Column(Boolean, default=True)
    telegram_viral_threshold = Column(Float, default=5.0)
    telegram_notify_errors = Column(Boolean, default=True)
    telegram_threshold_views = Column(Integer, default=10000)
    telegram_threshold_likes = Column(Integer, default=500)
    telegram_threshold_comments = Column(Integer, default=100)

    # Apify API token (для импорта рилсов с аккаунтов через Apify actor)
    apify_token = Column(String(255), nullable=True)

    # Кредитный баланс (копейки). Append-only ledger в credit_transactions
    # — источник истины; это поле для быстрого чтения. Синхронизируется
    # в той же транзакции что и запись в ledger.
    credits_balance_kopecks = Column(BigInteger, default=0, nullable=False)

    # Provider tokens (per-user, юзер платит провайдеру своим токеном).
    runway_api_key = Column(String(255), nullable=True)
    elevenlabs_api_key = Column(String(255), nullable=True)
    openai_api_key = Column(String(255), nullable=True)

    # Relationships
    reels = relationship("Reel", back_populates="user", cascade="all, delete-orphan")
    parse_jobs = relationship("ParseJob", back_populates="user", cascade="all, delete-orphan")
    generated_videos = relationship("GeneratedVideo", back_populates="user", cascade="all, delete-orphan")
    posting_targets = relationship("PostingTarget", back_populates="user", cascade="all, delete-orphan")
    posts = relationship("Post", back_populates="user", cascade="all, delete-orphan")
    credit_transactions = relationship("CreditTransaction", back_populates="user", cascade="all, delete-orphan")
    content_recipes = relationship("ContentRecipe", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User {self.email} ({self.tariff.value})>"
