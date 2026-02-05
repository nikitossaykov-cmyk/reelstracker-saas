"""
API для управления рилсами: CRUD
"""

from typing import List
from fastapi import APIRouter, Depends, status, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.reel import ReelCreate, ReelUpdate, ReelResponse, ReelHistoryResponse
from app.services.reel_service import (
    get_user_reels,
    get_reel_by_id,
    create_reel,
    update_reel,
    delete_reel,
    get_reel_history,
)
from app.services.parsing_service import create_parse_job

router = APIRouter()


@router.get("", response_model=List[ReelResponse])
def list_reels(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Список всех рилсов текущего юзера"""
    return get_user_reels(db, current_user)


@router.post("", response_model=ReelResponse, status_code=status.HTTP_201_CREATED)
def add_reel(
    data: ReelCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Добавить новый рилс + поставить в очередь на парсинг"""
    reel = create_reel(db, current_user, data)

    # Сразу ставим на парсинг
    create_parse_job(db, current_user, reel)

    return reel


@router.get("/{reel_id}", response_model=ReelResponse)
def get_reel(
    reel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Получить конкретный рилс"""
    return get_reel_by_id(db, reel_id, current_user)


@router.put("/{reel_id}", response_model=ReelResponse)
def edit_reel(
    reel_id: int,
    data: ReelUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Обновить рилс (title, enabled)"""
    return update_reel(db, reel_id, current_user, data)


@router.delete("/{reel_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_reel(
    reel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удалить рилс"""
    delete_reel(db, reel_id, current_user)


@router.get("/{reel_id}/history", response_model=List[ReelHistoryResponse])
def reel_history(
    reel_id: int,
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """История метрик рилса"""
    return get_reel_history(db, reel_id, current_user, limit)
