"""
Сервис для работы с рилсами: CRUD + бизнес-логика
"""

from typing import List, Optional
from sqlalchemy.orm import Session, joinedload
from fastapi import HTTPException, status

from app.models.user import User
from app.models.reel import Reel, ReelHistory
from app.schemas.reel import ReelCreate, ReelUpdate
from app.services.tariff_service import can_add_reel


def get_user_reels(db: Session, user: User) -> List[Reel]:
    """Получить все рилсы юзера с историей"""
    return (
        db.query(Reel)
        .options(joinedload(Reel.history))
        .filter(Reel.user_id == user.id)
        .order_by(Reel.created_at.desc())
        .all()
    )


def get_reel_by_id(db: Session, reel_id: int, user: User) -> Reel:
    """Получить рилс по ID (с проверкой владельца)"""
    reel = (
        db.query(Reel)
        .options(joinedload(Reel.history))
        .filter(Reel.id == reel_id, Reel.user_id == user.id)
        .first()
    )
    if not reel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Рилс не найден",
        )
    return reel


def create_reel(db: Session, user: User, data: ReelCreate) -> Reel:
    """Создать новый рилс"""

    # Проверка лимита тарифа
    if not can_add_reel(user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Достигнут лимит рилсов на вашем тарифе. Обновите до Pro.",
        )

    # Проверка дубликата URL для этого юзера
    existing = db.query(Reel).filter(
        Reel.user_id == user.id,
        Reel.url == data.url,
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Этот рилс уже отслеживается",
        )

    reel = Reel(
        user_id=user.id,
        title=data.title,
        platform=data.platform,
        url=data.url,
    )
    db.add(reel)
    db.commit()
    db.refresh(reel)
    return reel


def update_reel(db: Session, reel_id: int, user: User, data: ReelUpdate) -> Reel:
    """Обновить рилс (title, enabled)"""
    reel = get_reel_by_id(db, reel_id, user)

    if data.title is not None:
        reel.title = data.title
    if data.enabled is not None:
        reel.enabled = data.enabled

    db.commit()
    db.refresh(reel)
    return reel


def delete_reel(db: Session, reel_id: int, user: User) -> bool:
    """Удалить рилс (с каскадом истории и задач)"""
    reel = get_reel_by_id(db, reel_id, user)
    db.delete(reel)
    db.commit()
    return True


def get_reel_history(db: Session, reel_id: int, user: User, limit: int = 50) -> List[ReelHistory]:
    """Получить историю метрик рилса"""
    # Проверяем, что рилс принадлежит юзеру
    reel = get_reel_by_id(db, reel_id, user)

    return (
        db.query(ReelHistory)
        .filter(ReelHistory.reel_id == reel.id)
        .order_by(ReelHistory.parsed_at.desc())
        .limit(limit)
        .all()
    )
