"""
API настроек Telegram: получение, обновление, тест
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.telegram import TelegramSettings, TelegramSettingsUpdate
from app.services.telegram_service import (
    get_user_telegram,
    update_telegram_settings,
    TelegramService,
)

router = APIRouter()


@router.get("", response_model=TelegramSettings)
def get_telegram_settings(current_user: User = Depends(get_current_user)):
    """Получить текущие настройки Telegram"""
    return TelegramSettings(
        enabled=current_user.telegram_enabled,
        bot_token=current_user.telegram_bot_token,
        chat_id=current_user.telegram_chat_id,
        notify_start=current_user.telegram_notify_start,
        notify_complete=current_user.telegram_notify_complete,
        notify_viral=current_user.telegram_notify_viral,
        viral_threshold=current_user.telegram_viral_threshold,
        notify_errors=current_user.telegram_notify_errors,
        threshold_views=current_user.telegram_threshold_views,
        threshold_likes=current_user.telegram_threshold_likes,
        threshold_comments=current_user.telegram_threshold_comments,
    )


@router.put("", response_model=TelegramSettings)
def update_settings(
    data: TelegramSettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Обновить настройки Telegram"""
    user = update_telegram_settings(db, current_user, data.model_dump(exclude_none=True))

    return TelegramSettings(
        enabled=user.telegram_enabled,
        bot_token=user.telegram_bot_token,
        chat_id=user.telegram_chat_id,
        notify_start=user.telegram_notify_start,
        notify_complete=user.telegram_notify_complete,
        notify_viral=user.telegram_notify_viral,
        viral_threshold=user.telegram_viral_threshold,
        notify_errors=user.telegram_notify_errors,
        threshold_views=user.telegram_threshold_views,
        threshold_likes=user.telegram_threshold_likes,
        threshold_comments=user.telegram_threshold_comments,
    )


@router.post("/test")
async def test_telegram(current_user: User = Depends(get_current_user)):
    """Отправить тестовое сообщение в Telegram"""
    tg = get_user_telegram(current_user)
    if not tg:
        return {"success": False, "error": "Telegram не настроен. Укажите bot_token и chat_id."}

    # Тест подключения
    conn = await tg.test_connection()
    if not conn["success"]:
        return {"success": False, "error": f"Не удалось подключиться к боту: {conn['error']}"}

    # Отправить тестовое сообщение
    sent = await tg.send_message(
        f"✅ <b>Тест успешен!</b>\n\n"
        f"Бот <code>@{conn['bot_username']}</code> подключен.\n"
        f"Уведомления будут приходить в этот чат."
    )

    return {
        "success": sent,
        "bot_username": conn.get("bot_username"),
    }
