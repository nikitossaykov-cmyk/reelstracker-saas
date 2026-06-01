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
from app.core.token_crypto import encrypt_token, decrypt_token, is_encrypted

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
    pt = PostingTarget(
        user_id=user.id,
        platform=platform,
        platform_account_id=platform_account_id,
        platform_username=platform_username,
        access_token_encrypted=encrypt_token(access_token),
        refresh_token_encrypted=encrypt_token(refresh_token),
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
    return decrypt_token(target.access_token_encrypted) or ""


def get_refresh_token(target: PostingTarget) -> str:
    return decrypt_token(target.refresh_token_encrypted) or ""


def migrate_legacy_plaintext_tokens(db: Session) -> int:
    """One-shot: encrypt any PostingTarget rows still holding plaintext.

    Detect via Fernet prefix. Safe to call repeatedly — idempotent.
    Returns count of rows rewritten.
    """
    rows = db.query(PostingTarget).all()
    n = 0
    for r in rows:
        changed = False
        if r.access_token_encrypted and not is_encrypted(r.access_token_encrypted):
            r.access_token_encrypted = encrypt_token(r.access_token_encrypted)
            changed = True
        if r.refresh_token_encrypted and not is_encrypted(r.refresh_token_encrypted):
            r.refresh_token_encrypted = encrypt_token(r.refresh_token_encrypted)
            changed = True
        if changed:
            n += 1
    if n:
        db.commit()
        logger.info(f"migrate_legacy_plaintext_tokens: re-encrypted {n} rows")
    return n
