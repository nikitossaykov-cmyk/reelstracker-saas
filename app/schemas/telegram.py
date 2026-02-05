"""
Pydantic схемы для настроек Telegram
"""

from pydantic import BaseModel
from typing import Optional


class TelegramSettings(BaseModel):
    enabled: bool
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    notify_start: bool = True
    notify_complete: bool = True
    notify_viral: bool = True
    viral_threshold: float = 5.0
    notify_errors: bool = True
    threshold_views: int = 10000
    threshold_likes: int = 500
    threshold_comments: int = 100

    model_config = {"from_attributes": True}


class TelegramSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    notify_start: Optional[bool] = None
    notify_complete: Optional[bool] = None
    notify_viral: Optional[bool] = None
    viral_threshold: Optional[float] = None
    notify_errors: Optional[bool] = None
    threshold_views: Optional[int] = None
    threshold_likes: Optional[int] = None
    threshold_comments: Optional[int] = None
