"""
Pydantic схемы для рилсов
"""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List


class ReelCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    platform: str = Field(pattern="^(instagram|tiktok|youtube|vk)$")
    url: str = Field(min_length=10, max_length=1024)


class ReelUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    enabled: Optional[bool] = None


class ReelHistoryResponse(BaseModel):
    id: int
    views: int
    likes: int
    comments: int
    shares: int
    parsed_at: datetime

    model_config = {"from_attributes": True}


class ReelResponse(BaseModel):
    id: int
    title: str
    platform: str
    url: str
    enabled: bool
    views: int
    likes: int
    comments: int
    shares: int
    thumbnail_url: Optional[str] = None
    author_username: Optional[str] = None
    author_full_name: Optional[str] = None
    published_at: Optional[datetime] = None
    caption: Optional[str] = None
    duration_seconds: Optional[float] = None
    instagram_account_id: Optional[int] = None
    position_in_account: Optional[int] = None
    last_parsed_at: Optional[datetime] = None
    created_at: datetime
    history: List[ReelHistoryResponse] = []

    model_config = {"from_attributes": True}
