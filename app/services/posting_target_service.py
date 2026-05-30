"""
Posting target service — CRUD для PostingTarget (IG/TT/VK/YT-аккаунты
с OAuth-credentials).
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.generation import PostingTarget, PostingPlatform

logger = logging.getLogger(__name__)


def create_posting_target(
    db: Session,
    user: User,
    *,
    platform: PostingPlatform,
    platform_account_id: str,
    platform_username: Optional[str] = None,
    access_token: str,
    refresh_token: Optional[str] = None,
    default_caption_template: Optional[str] = None,
) -> PostingTarget:
    """Создать posting target.

    TODO в следующих PR: шифрование access_token через Fernet с ключом
    из env. Сейчас храним как есть (Postgres-side TDE / Railway encrypted
    storage уже даёт RGE-encryption-at-rest).
    """
    pt = PostingTarget(
        user_id=user.id,
        platform=platform,
        platform_account_id=platform_account_id,
        platform_username=platform_username,
        access_token_encrypted=access_token,  # TODO: encrypt before store
        refresh_token_encrypted=refresh_token,
        default_caption_template=default_caption_template,
    )
    db.add(pt)
    db.commit()
    db.refresh(pt)
    logger.info(f"✅ PostingTarget #{pt.id} ({platform.value}@{platform_username}) for user_id={user.id}")
    return pt


def list_user_targets(db: Session, user: User) -> list[PostingTarget]:
    return (db.query(PostingTarget)
            .filter(PostingTarget.user_id == user.id)
            .order_by(PostingTarget.created_at.desc()).all())


def get_target_by_id(
    db: Session, target_id: int, user: User,
) -> Optional[PostingTarget]:
    return db.query(PostingTarget).filter(
        PostingTarget.id == target_id,
        PostingTarget.user_id == user.id,
    ).first()


def delete_target(db: Session, target: PostingTarget) -> None:
    db.delete(target)
    db.commit()


def get_access_token(target: PostingTarget) -> str:
    """Расшифровать access_token (пока no-op — TODO Fernet)."""
    return target.access_token_encrypted or ""
