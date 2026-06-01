"""
API парсинга: постановка в очередь, статус
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.services.reel_service import get_reel_by_id
from app.services.parsing_service import (
    create_parse_job,
    create_parse_jobs_for_user,
    get_parse_status,
)

router = APIRouter()


@router.post("")
def start_parsing(
    reel_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Поставить рилс(ы) в очередь на парсинг.
    Если reel_id указан — только этот рилс.
    Если не указан — все активные рилсы юзера.
    """
    # Проверяем, можно ли парсить
    parse_status = get_parse_status(db, current_user)
    if not parse_status["can_parse"]:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Слишком часто. Следующий парсинг доступен: {parse_status['next_allowed']}",
        )

    if reel_id:
        reel = get_reel_by_id(db, reel_id, current_user)
        job = create_parse_job(db, current_user, reel)
        return {
            "success": True,
            "jobs_created": 1,
            "message": f"Рилс '{reel.title}' поставлен в очередь",
        }
    else:
        jobs = create_parse_jobs_for_user(db, current_user)
        return {
            "success": True,
            "jobs_created": len(jobs),
            "message": f"В очередь поставлено: {len(jobs)} рилсов",
        }


@router.get("/status")
def parsing_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Статус очереди парсинга для текущего юзера"""
    return get_parse_status(db, current_user)
