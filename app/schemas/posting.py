"""
Pydantic схемы для Post / PostingTarget.
"""

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field

from app.models.generation import PostingPlatform, PostStatus


class PostingTargetCreate(BaseModel):
    platform: PostingPlatform = PostingPlatform.INSTAGRAM
    platform_account_id: str = Field(min_length=1, max_length=255)
    platform_username: Optional[str] = Field(None, max_length=255)
    access_token: str = Field(min_length=10, max_length=4000)
    refresh_token: Optional[str] = Field(None, max_length=4000)
    default_caption_template: Optional[str] = Field(None, max_length=2200)


class PostingTargetResponse(BaseModel):
    id: int
    platform: PostingPlatform
    platform_account_id: str
    platform_username: Optional[str] = None
    posting_enabled: bool
    default_caption_template: Optional[str] = None
    created_at: datetime
    last_used_at: Optional[datetime] = None
    has_token: bool = True

    model_config = {"from_attributes": True}


class PostCreate(BaseModel):
    generated_video_id: int
    posting_target_id: int
    caption: Optional[str] = Field(None, max_length=2200)
    scheduled_for: Optional[datetime] = None
    publish_now: bool = False


class PostResponse(BaseModel):
    id: int
    generated_video_id: int
    posting_target_id: int
    status: PostStatus
    caption: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    published_at: Optional[datetime] = None
    platform_post_id: Optional[str] = None
    platform_url: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PostListResponse(BaseModel):
    items: List[PostResponse]
    total: int
