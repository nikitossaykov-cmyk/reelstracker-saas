"""
Сервис Telegram уведомлений per-user
"""

import logging
import httpx
from typing import Optional
from sqlalchemy.orm import Session

from app.models.user import User

logger = logging.getLogger(__name__)


class TelegramService:
    """Обёртка для отправки уведомлений конкретному юзеру"""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}"

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Отправить сообщение в Telegram"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{self.api_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                    },
                )
                if response.status_code == 200:
                    logger.info(f"Telegram: сообщение отправлено в чат {self.chat_id}")
                    return True
                else:
                    logger.error(f"Telegram error: {response.text}")
                    return False
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    async def test_connection(self) -> dict:
        """Проверить подключение к боту"""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.api_url}/getMe")
                if response.status_code == 200:
                    data = response.json()
                    return {
                        "success": True,
                        "bot_username": data["result"]["username"],
                    }
                return {"success": False, "error": f"HTTP {response.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_user_telegram(user: User) -> Optional[TelegramService]:
    """Получить TelegramService для юзера, если настроен"""
    if not user.telegram_enabled or not user.telegram_bot_token or not user.telegram_chat_id:
        return None
    return TelegramService(user.telegram_bot_token, user.telegram_chat_id)


def update_telegram_settings(db: Session, user: User, settings: dict) -> User:
    """Обновить настройки Telegram для юзера"""
    field_mapping = {
        "enabled": "telegram_enabled",
        "bot_token": "telegram_bot_token",
        "chat_id": "telegram_chat_id",
        "notify_start": "telegram_notify_start",
        "notify_complete": "telegram_notify_complete",
        "notify_viral": "telegram_notify_viral",
        "viral_threshold": "telegram_viral_threshold",
        "notify_errors": "telegram_notify_errors",
        "threshold_views": "telegram_threshold_views",
        "threshold_likes": "telegram_threshold_likes",
        "threshold_comments": "telegram_threshold_comments",
    }

    for key, value in settings.items():
        if value is not None and key in field_mapping:
            setattr(user, field_mapping[key], value)

    db.commit()
    db.refresh(user)
    return user
