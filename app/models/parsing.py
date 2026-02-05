"""
Модель очереди парсинга
"""

import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum, Text, Index
from sqlalchemy.orm import relationship
from app.database import Base


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ParseJob(Base):
    __tablename__ = "parse_jobs"

    id = Column(Integer, primary_key=True, index=True)
    reel_id = Column(Integer, ForeignKey("reels.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    status = Column(Enum(JobStatus), default=JobStatus.PENDING, nullable=False, index=True)
    priority = Column(Integer, default=0)  # Pro=10, Free=0

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    error_message = Column(Text, nullable=True)

    # Результат парсинга
    result_views = Column(Integer, nullable=True)
    result_likes = Column(Integer, nullable=True)
    result_comments = Column(Integer, nullable=True)
    result_shares = Column(Integer, nullable=True)

    # Relationships
    reel = relationship("Reel", back_populates="parse_jobs")
    user = relationship("User", back_populates="parse_jobs")

    __table_args__ = (
        Index("ix_parse_jobs_status_priority", "status", "priority"),
    )

    def __repr__(self):
        return f"<ParseJob {self.id} reel={self.reel_id} status={self.status.value}>"
