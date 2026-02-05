"""
Модель пользователя
"""

import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum, Float
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

    # Relationships
    reels = relationship("Reel", back_populates="user", cascade="all, delete-orphan")
    parse_jobs = relationship("ParseJob", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User {self.email} ({self.tariff.value})>"
