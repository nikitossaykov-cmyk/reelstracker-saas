"""
Posting service — CRUD для Post + создание POST_TO_INSTAGRAM job'ов.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.user import User
from app.models.generation import (
    GeneratedVideo, PostingTarget, Post, PostStatus,
)
from app.models.parsing import ParseJob, JobStatus, JobType
from app.services.tariff_service import get_priority

logger = logging.getLogger(__name__)


def create_post(
    db: Session,
    user: User,
    generated_video: GeneratedVideo,
    posting_target: PostingTarget,
    caption: Optional[str] = None,
    scheduled_for: Optional[datetime] = None,
    publish_now: bool = False,
) -> Post:
    """Создать Post (DRAFT). Если publish_now=True — поставить
    POST_TO_INSTAGRAM job сразу."""
    post = Post(
        user_id=user.id,
        generated_video_id=generated_video.id,
        posting_target_id=posting_target.id,
        status=PostStatus.SCHEDULED if (scheduled_for or publish_now) else PostStatus.DRAFT,
        caption=caption,
        scheduled_for=scheduled_for or (datetime.utcnow() if publish_now else None),
    )
    db.add(post)
    db.flush()

    if publish_now:
        _enqueue_post_job(db, user, post)

    db.commit()
    db.refresh(post)
    return post


def _enqueue_post_job(db: Session, user: User, post: Post) -> ParseJob:
    job = ParseJob(
        reel_id=None,
        user_id=user.id,
        post_id=post.id,
        job_type=JobType.POST_TO_INSTAGRAM,
        status=JobStatus.PENDING,
        priority=get_priority(user),
    )
    db.add(job)
    db.flush()
    logger.info(f"✅ POST_TO_INSTAGRAM job #{job.id} for post #{post.id}")
    return job


def publish_post_now(db: Session, user: User, post: Post) -> ParseJob:
    """Перевести DRAFT/SCHEDULED → enqueue POST_TO_INSTAGRAM."""
    if post.status not in (PostStatus.DRAFT, PostStatus.SCHEDULED):
        raise ValueError(f"Post #{post.id} в статусе {post.status.value}, "
                         "можно публиковать только из DRAFT/SCHEDULED")
    post.status = PostStatus.SCHEDULED
    post.scheduled_for = post.scheduled_for or datetime.utcnow()
    job = _enqueue_post_job(db, user, post)
    db.commit()
    return job


def list_user_posts(
    db: Session, user: User, limit: int = 50, offset: int = 0,
    status_filter: Optional[PostStatus] = None,
) -> tuple[list[Post], int]:
    q = db.query(Post).filter(Post.user_id == user.id)
    if status_filter:
        q = q.filter(Post.status == status_filter)
    total = q.with_entities(func.count(Post.id)).scalar() or 0
    items = (q.order_by(Post.created_at.desc())
             .limit(limit).offset(offset).all())
    return items, total


def get_post_by_id(db: Session, post_id: int, user: User) -> Optional[Post]:
    return db.query(Post).filter(
        Post.id == post_id, Post.user_id == user.id
    ).first()


def mark_post_publishing(db: Session, post: Post) -> None:
    post.status = PostStatus.PUBLISHING
    db.commit()


def mark_post_published(
    db: Session, post: Post,
    platform_post_id: str, platform_url: Optional[str] = None,
) -> None:
    post.status = PostStatus.PUBLISHED
    post.platform_post_id = platform_post_id
    post.platform_url = platform_url
    post.published_at = datetime.utcnow()
    post.error_message = None
    db.commit()


def mark_post_failed(db: Session, post: Post, error: str) -> None:
    post.status = PostStatus.FAILED
    post.error_message = (error or "")[:2000]
    db.commit()
