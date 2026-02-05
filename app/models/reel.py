"""
Модели рилсов и истории метрик
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, UniqueConstraint, Index
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

    last_parsed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    user = relationship("User", back_populates="reels")
    history = relationship("ReelHistory", back_populates="reel", cascade="all, delete-orphan",
                           order_by="ReelHistory.parsed_at.asc()")
    parse_jobs = relationship("ParseJob", back_populates="reel", cascade="all, delete-orphan")

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
