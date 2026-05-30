"""
API для Post — создание, список, publish-now.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.generation import PostStatus, GeneratedVideo
from app.services.generation_service import get_generation_by_id
from app.services.posting_target_service import get_target_by_id
from app.services.posting_service import (
    create_post, publish_post_now,
    list_user_posts, get_post_by_id,
)
from app.schemas.posting import (
    PostCreate, PostResponse, PostListResponse,
)

router = APIRouter()


@router.post("", response_model=PostResponse, status_code=status.HTTP_201_CREATED)
def add_post(
    data: PostCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    gv = get_generation_by_id(db, data.generated_video_id, current_user)
    if not gv:
        raise HTTPException(404, detail=f"GeneratedVideo #{data.generated_video_id} не найден")
    target = get_target_by_id(db, data.posting_target_id, current_user)
    if not target:
        raise HTTPException(404, detail=f"PostingTarget #{data.posting_target_id} не найден")
    return create_post(
        db, current_user,
        generated_video=gv,
        posting_target=target,
        caption=data.caption,
        scheduled_for=data.scheduled_for,
        publish_now=data.publish_now,
    )


@router.get("", response_model=PostListResponse)
def list_posts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: Optional[PostStatus] = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items, total = list_user_posts(db, current_user, limit=limit, offset=offset,
                                   status_filter=status_filter)
    return PostListResponse(items=items, total=total)


@router.get("/{post_id}", response_model=PostResponse)
def get_post(
    post_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = get_post_by_id(db, post_id, current_user)
    if not p:
        raise HTTPException(404, detail=f"Post #{post_id} не найден")
    return p


@router.post("/{post_id}/publish", response_model=PostResponse)
def publish_post(
    post_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Перевести Post в SCHEDULED и enqueue POST_TO_INSTAGRAM job."""
    p = get_post_by_id(db, post_id, current_user)
    if not p:
        raise HTTPException(404, detail=f"Post #{post_id} не найден")
    try:
        publish_post_now(db, current_user, p)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    db.refresh(p)
    return p
