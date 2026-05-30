"""
Worker для POST_TO_INSTAGRAM задач.

Берёт Post → его PostingTarget + GeneratedVideo → дёргает IG Graph API
через InstagramPublisher → сохраняет platform_post_id.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.parsing import ParseJob
from app.models.generation import Post, PostingPlatform
from app.services.parsing_service import complete_job, fail_job
from app.services.posting_service import (
    mark_post_publishing, mark_post_published, mark_post_failed,
)
from app.services.posting_target_service import get_access_token
from app.core.publishers import get_publisher, PublishError, OAuthExpired

logger = logging.getLogger(__name__)


def process_post_to_instagram_job(db: Session, job: ParseJob) -> bool:
    post = db.query(Post).filter(Post.id == job.post_id).first()
    if not post:
        fail_job(db, job, "Post не найден")
        return True

    target = post.posting_target
    gv = post.generated_video
    if not target:
        msg = f"Post #{post.id} без posting_target"
        fail_job(db, job, msg); mark_post_failed(db, post, msg)
        return True
    if not gv:
        msg = f"Post #{post.id} без generated_video"
        fail_job(db, job, msg); mark_post_failed(db, post, msg)
        return True

    # Используем uniq-копию если есть, иначе оригинал
    media_url = gv.uniq_media_url or gv.media_url
    if not media_url:
        msg = f"GeneratedVideo #{gv.id} ещё не имеет media_url (status={gv.status.value})"
        fail_job(db, job, msg); mark_post_failed(db, post, msg)
        return True

    # Map target.platform → publisher name
    platform_to_pub = {
        PostingPlatform.INSTAGRAM: "instagram",
        PostingPlatform.TIKTOK: "tiktok",
        PostingPlatform.VK_CLIPS: "vk_clips",
        PostingPlatform.YOUTUBE_SHORTS: "youtube_shorts",
    }
    publisher_name = platform_to_pub.get(target.platform)
    if not publisher_name:
        msg = f"Unsupported platform {target.platform.value}"
        fail_job(db, job, msg); mark_post_failed(db, post, msg)
        return True

    logger.info(f"📤 POST_TO_{target.platform.value.upper()} #{job.id} → "
                f"post #{post.id} → target #{target.id}")
    mark_post_publishing(db, post)

    try:
        publisher = get_publisher(publisher_name)
        result = publisher.publish_video(
            media_url=media_url,
            caption=post.caption or "",
            platform_account_id=target.platform_account_id,
            access_token=get_access_token(target),
        )
    except OAuthExpired as e:
        msg = f"OAuth expired: {e} — обновить access_token у target #{target.id}"
        fail_job(db, job, msg); mark_post_failed(db, post, msg)
        return True
    except PublishError as e:
        msg = f"IG publish failed: {e}"
        fail_job(db, job, msg); mark_post_failed(db, post, msg)
        return True
    except Exception as e:
        msg = f"unexpected: {type(e).__name__}: {str(e)[:300]}"
        fail_job(db, job, msg); mark_post_failed(db, post, msg)
        return True

    mark_post_published(db, post, result.platform_post_id, result.platform_url)
    complete_job(db, job, 0, 0, 0, 0)
    logger.info(f"✅ Post #{post.id} published → {result.platform_url}")
    return True
