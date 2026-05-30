"""
Модели для генерации видео и публикации в соцсети.

PR #1: только модели + миграция. Workers/UI/billing-logic — в следующих PR.
"""

import enum
from datetime import datetime
from sqlalchemy import (
    Column, Integer, BigInteger, String, Boolean, DateTime,
    ForeignKey, Enum, Text, JSON, Index, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.database import Base


class GenerationStatus(str, enum.Enum):
    PENDING = "pending"          # в очереди
    RUNNING = "running"          # провайдер генерирует
    UPLOADING = "uploading"      # скачиваем результат → S3/R2
    READY = "ready"              # медиа готова, можно публиковать
    FAILED = "failed"


class VideoProvider(str, enum.Enum):
    RUNWAY = "runway"
    PIKA = "pika"
    HAILUO = "hailuo"
    VEO = "veo"
    MOCK = "mock"  # для тестов без обращения к API


class GeneratedVideo(Base):
    """Сгенерированное видео.

    Один промпт + параметры провайдера → один media-объект в нашем хранилище.
    Может быть переиспользован в нескольких Post-ах (например, кросс-постинг
    одного видео в IG и TT).
    """
    __tablename__ = "generated_videos"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)

    prompt = Column(Text, nullable=False)
    provider = Column(Enum(VideoProvider), nullable=False)
    status = Column(Enum(GenerationStatus), default=GenerationStatus.PENDING,
                    nullable=False, index=True)

    # Параметры запроса к провайдеру (model, seed, duration, aspect_ratio, ...).
    # Храним JSON чтобы дергать одинаковую генерацию через retry/clone.
    provider_params = Column(JSON, nullable=True)

    # Внешний ID задачи у провайдера (для polling-а статуса).
    provider_job_id = Column(String(255), nullable=True, index=True)

    # Сколько списали с баланса юзера (в копейках, signed: -cost_kopecks).
    cost_kopecks = Column(BigInteger, nullable=True)

    # Если это remake — указатели на источник.
    source_reel_id = Column(Integer, ForeignKey("reels.id", ondelete="SET NULL"), nullable=True, index=True)
    source_recipe_id = Column(Integer, ForeignKey("content_recipes.id", ondelete="SET NULL"), nullable=True, index=True)

    # Финальная медиа после загрузки в наше хранилище.
    media_url = Column(String(1024), nullable=True)       # публичный URL для IG Graph API
    media_storage_key = Column(String(512), nullable=True)  # ключ в R2/S3 для cleanup
    thumbnail_url = Column(String(1024), nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)

    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="generated_videos")
    posts = relationship("Post", back_populates="generated_video",
                         cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_generated_videos_user_status", "user_id", "status"),
    )

    def __repr__(self):
        return f"<GeneratedVideo {self.id} user={self.user_id} status={self.status.value}>"


class PostingPlatform(str, enum.Enum):
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    YOUTUBE_SHORTS = "youtube_shorts"
    VK_CLIPS = "vk_clips"


class PostingTarget(Base):
    """Аккаунт куда мы можем публиковать (с OAuth токеном).

    Отдельно от InstagramAccount (тот для трекинга чужих/своих профилей):
    PostingTarget хранит posting-credentials. У одного юзера может быть
    несколько target-ов (например, два IG business-аккаунта).
    """
    __tablename__ = "posting_targets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)

    platform = Column(Enum(PostingPlatform), nullable=False)

    # Идентификатор аккаунта на платформе (ig business account id, tt open_id, ...).
    platform_account_id = Column(String(255), nullable=False)
    # Display-имя (для UI).
    platform_username = Column(String(255), nullable=True)

    # OAuth-токены. Шифрование на стороне приложения (в следующем PR — Fernet
    # с ключом из env). Сейчас просто Text — никаких токенов в БД ещё нет.
    access_token_encrypted = Column(Text, nullable=True)
    refresh_token_encrypted = Column(Text, nullable=True)
    token_expires_at = Column(DateTime, nullable=True)

    posting_enabled = Column(Boolean, default=True, nullable=False)
    default_caption_template = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="posting_targets")
    posts = relationship("Post", back_populates="posting_target",
                         cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("user_id", "platform", "platform_account_id",
                         name="uq_user_platform_account"),
    )

    def __repr__(self):
        return f"<PostingTarget {self.id} {self.platform.value}@{self.platform_username}>"


class PostStatus(str, enum.Enum):
    DRAFT = "draft"              # только создан, ещё не запланирован
    SCHEDULED = "scheduled"      # запланирован на scheduled_for
    PUBLISHING = "publishing"    # worker публикует (Graph API)
    PUBLISHED = "published"      # успешно опубликован
    FAILED = "failed"


class Post(Base):
    """Конкретная публикация: видео → платформа → конкретный момент."""
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    generated_video_id = Column(Integer, ForeignKey("generated_videos.id", ondelete="CASCADE"),
                                nullable=False, index=True)
    posting_target_id = Column(Integer, ForeignKey("posting_targets.id", ondelete="CASCADE"),
                               nullable=False, index=True)

    status = Column(Enum(PostStatus), default=PostStatus.DRAFT,
                    nullable=False, index=True)

    caption = Column(Text, nullable=True)

    scheduled_for = Column(DateTime, nullable=True, index=True)
    published_at = Column(DateTime, nullable=True)

    # ID и URL поста на платформе (после успешной публикации).
    platform_post_id = Column(String(255), nullable=True)
    platform_url = Column(String(1024), nullable=True)

    # Если хотим автотрекинг своих публикаций через Reelstracker — храним
    # FK на Reel, который будет создан при первой синхронизации. nullable.
    tracked_reel_id = Column(Integer, ForeignKey("reels.id", ondelete="SET NULL"),
                             nullable=True)

    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="posts")
    generated_video = relationship("GeneratedVideo", back_populates="posts")
    posting_target = relationship("PostingTarget", back_populates="posts")

    __table_args__ = (
        Index("ix_posts_status_scheduled", "status", "scheduled_for"),
    )

    def __repr__(self):
        return f"<Post {self.id} {self.status.value} → {self.posting_target_id}>"
