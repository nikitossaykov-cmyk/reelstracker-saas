"""
API тарифных планов
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.services.tariff_service import get_tariff_info, upgrade_to_pro

router = APIRouter()


@router.get("")
def get_tariff(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Текущий тариф + лимиты + использование"""
    return get_tariff_info(current_user, db)


@router.post("/upgrade")
def upgrade(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Апгрейд до Pro (MVP: без оплаты)"""
    user = upgrade_to_pro(current_user, db)
    return {
        "success": True,
        "tariff": user.tariff.value,
        "message": "Тариф обновлён до Pro!",
    }
