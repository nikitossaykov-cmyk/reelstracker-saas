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


class JobType(str, enum.Enum):
    PARSE_REEL = "parse_reel"             # одиночный парсинг рилса (legacy default)
    SYNC_ACCOUNT = "sync_account"         # импорт списка рилсов с Instagram-аккаунта
    GENERATE_VIDEO = "generate_video"     # запросить генерацию у внешнего провайдера
    POST_TO_INSTAGRAM = "post_to_instagram"  # опубликовать через IG Graph API
    OAUTH_REFRESH = "oauth_refresh"       # обновить access_token у PostingTarget
    ANALYZE_REEL = "analyze_reel"         # Whisper + Vision + scenes + hook classification
    REMAKE_VIDEO = "remake_video"         # PR #6: гибридная генерация по recipe


class ParseJob(Base):
    __tablename__ = "parse_jobs"

    id = Column(Integer, primary_key=True, index=True)
    # Для SYNC_ACCOUNT reel_id не нужен — делаем nullable
    reel_id = Column(Integer, ForeignKey("reels.id", ondelete="CASCADE"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Для SYNC_ACCOUNT — ссылка на аккаунт
    account_id = Column(Integer, ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=True)
    # Для GENERATE_VIDEO — запись в generated_videos
    generated_video_id = Column(Integer, ForeignKey("generated_videos.id", ondelete="CASCADE"), nullable=True)
    # Для POST_TO_INSTAGRAM — конкретный пост
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=True)
    # Для OAUTH_REFRESH — целевой posting target
    posting_target_id = Column(Integer, ForeignKey("posting_targets.id", ondelete="CASCADE"), nullable=True)
    job_type = Column(Enum(JobType), default=JobType.PARSE_REEL, nullable=False)

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
