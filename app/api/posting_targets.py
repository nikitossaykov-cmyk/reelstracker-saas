"""
API для PostingTarget — добавление IG/TT/etc аккаунтов с access tokens.

В этом PR — manual entry: юзер сам получает long-lived token через
Graph API Explorer или OAuth flow на своей стороне, потом вставляет
в форму. Полноценный OAuth callback flow лежит в PR #11 (когда будет
Facebook App approved через Business Verification).
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.posting import (
    PostingTargetCreate, PostingTargetResponse,
)
from app.services.posting_target_service import (
    create_posting_target, list_user_targets,
    get_target_by_id, delete_target,
)

router = APIRouter()


def _to_response(target) -> PostingTargetResponse:
    return PostingTargetResponse(
        id=target.id,
        platform=target.platform,
        platform_account_id=target.platform_account_id,
        platform_username=target.platform_username,
        posting_enabled=target.posting_enabled,
        default_caption_template=target.default_caption_template,
        created_at=target.created_at,
        last_used_at=target.last_used_at,
        has_token=bool(target.access_token_encrypted),
    )


@router.post("", response_model=PostingTargetResponse, status_code=status.HTTP_201_CREATED)
def add_target(
    data: PostingTargetCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    target = create_posting_target(
        db, current_user,
        platform=data.platform,
        platform_account_id=data.platform_account_id,
        platform_username=data.platform_username,
        access_token=data.access_token,
        refresh_token=data.refresh_token,
        default_caption_template=data.default_caption_template,
    )
    return _to_response(target)


@router.get("", response_model=List[PostingTargetResponse])
def list_targets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    targets = list_user_targets(db, current_user)
    return [_to_response(t) for t in targets]


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_target(
    target_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    t = get_target_by_id(db, target_id, current_user)
    if not t:
        raise HTTPException(404, detail="PostingTarget не найден")
    delete_target(db, t)
