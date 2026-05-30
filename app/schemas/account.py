"""
Pydantic-схемы для Instagram-аккаунтов
"""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class AccountCreate(BaseModel):
    username: str = Field(min_length=1, max_length=255, description="Instagram username без @")


class AccountResponse(BaseModel):
    id: int
    instagram_username: str
    instagram_user_id: Optional[str] = None
    full_name: Optional[str] = None
    profile_pic_url: Optional[str] = None
    bio: Optional[str] = None
    followers_count: Optional[int] = None
    following_count: Optional[int] = None
    posts_count: Optional[int] = None
    sync_enabled: bool
    last_synced_at: Optional[datetime] = None
    last_sync_error: Optional[str] = None
    auto_download_media: bool = False
    reels_count: int = 0  # заполняется в сервисе
    created_at: datetime

    model_config = {"from_attributes": True}


class AccountUpdate(BaseModel):
    """PATCH-like обновление флагов аккаунта."""
    sync_enabled: Optional[bool] = None
    auto_download_media: Optional[bool] = None
