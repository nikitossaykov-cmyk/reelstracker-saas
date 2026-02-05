"""
Сервис тарифных планов: лимиты, проверки
"""

from sqlalchemy.orm import Session
from app.models.user import User, TariffType
from app.models.reel import Reel
from app.config import get_settings

settings = get_settings()

TARIFF_LIMITS = {
    TariffType.FREE: {
        "max_reels": settings.FREE_MAX_REELS,
        "parse_interval_minutes": settings.FREE_PARSE_INTERVAL_MINUTES,
        "priority": 0,
        "label": "Free",
    },
    TariffType.PRO: {
        "max_reels": 999999,  # безлимит
        "parse_interval_minutes": settings.PRO_PARSE_INTERVAL_MINUTES,
        "priority": 10,
        "label": "Pro",
    },
}


def get_tariff_info(user: User, db: Session) -> dict:
    """Получить информацию о тарифе пользователя"""
    limits = TARIFF_LIMITS[user.tariff]
    reels_count = db.query(Reel).filter(Reel.user_id == user.id).count()

    return {
        "tariff": user.tariff.value,
        "label": limits["label"],
        "max_reels": limits["max_reels"],
        "reels_used": reels_count,
        "reels_remaining": max(0, limits["max_reels"] - reels_count),
        "parse_interval_minutes": limits["parse_interval_minutes"],
        "priority": limits["priority"],
    }


def can_add_reel(user: User, db: Session) -> bool:
    """Может ли юзер добавить ещё один рилс"""
    limits = TARIFF_LIMITS[user.tariff]
    reels_count = db.query(Reel).filter(Reel.user_id == user.id).count()
    return reels_count < limits["max_reels"]


def get_parse_interval(user: User) -> int:
    """Интервал парсинга в минутах для юзера"""
    return TARIFF_LIMITS[user.tariff]["parse_interval_minutes"]


def get_priority(user: User) -> int:
    """Приоритет в очереди парсинга"""
    return TARIFF_LIMITS[user.tariff]["priority"]


def upgrade_to_pro(user: User, db: Session) -> User:
    """Апгрейд на Pro (MVP: без оплаты)"""
    user.tariff = TariffType.PRO
    db.commit()
    db.refresh(user)
    return user
