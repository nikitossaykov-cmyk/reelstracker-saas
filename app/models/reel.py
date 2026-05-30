"""
Модели рилсов и истории метрик
"""

from datetime import datetime
from sqlalchemy import Column, BigInteger, Integer, String, Boolean, DateTime, ForeignKey, UniqueConstraint, Index, Text, Float
from sqlalchemy.orm import relationship
from app.database import Base


class Reel(Base):
    __tablename__ = "reels"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    platform = Column(String(50), nullable=False)  # instagram, tiktok, youtube, vk
    url = Column(String(1024), nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)

    # Текущие метрики (денормализация для быстрого доступа)
    views = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    shares = Column(Integer, default=0)

    # Метаданные рилса (заполняются парсером при первом успешном парсинге)
    thumbnail_url = Column(String(1024), nullable=True)
    author_username = Column(String(255), nullable=True)
    author_full_name = Column(String(255), nullable=True)
    published_at = Column(DateTime, nullable=True)   # когда рилс опубликован в Instagram
    caption = Column(Text, nullable=True)            # подпись
    duration_seconds = Column(Float, nullable=True)  # длительность видео

    # Bulk-импорт из аккаунта (опционально)
    instagram_account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    position_in_account = Column(Integer, nullable=True)  # 1 = самый свежий рилс на аккаунте

    last_parsed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Content Forge — кэш самого медиа в нашем R2 для downstream-работы
    # (анализ, ремейк, переозвучка). NULL пока не скачивали.
    media_storage_key = Column(String(512), nullable=True)
    media_size_bytes = Column(BigInteger, nullable=True)
    media_downloaded_at = Column(DateTime, nullable=True)
    # Последний известный source URL (IG-CDN). Протухает за часы, поэтому
    # download надо делать сразу после sync. Храним для отладки.
    media_source_url = Column(Text, nullable=True)
    media_download_error = Column(Text, nullable=True)

    # Relationships
    user = relationship("User", back_populates="reels")
    history = relationship("ReelHistory", back_populates="reel", cascade="all, delete-orphan",
                           order_by="ReelHistory.parsed_at.asc()")
    parse_jobs = relationship("ParseJob", back_populates="reel", cascade="all, delete-orphan")
    account = relationship("InstagramAccount", back_populates="reels")

    # Один URL на юзера
    __table_args__ = (
        UniqueConstraint("user_id", "url", name="uq_user_url"),
    )

    def __repr__(self):
        return f"<Reel {self.title} ({self.platform})>"


class ReelHistory(Base):
    __tablename__ = "reel_history"

    id = Column(Integer, primary_key=True, index=True)
    reel_id = Column(Integer, ForeignKey("reels.id", ondelete="CASCADE"), nullable=False)

    views = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    shares = Column(Integer, default=0)

    parsed_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    reel = relationship("Reel", back_populates="history")

    __table_args__ = (
        Index("ix_reel_history_reel_parsed", "reel_id", "parsed_at"),
    )

    def __repr__(self):
        return f"<ReelHistory reel={self.reel_id} views={self.views} at {self.parsed_at}>"
