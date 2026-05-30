"""
API для Instagram-аккаунтов: добавление, список, sync, удаление
"""

from typing import List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.account import InstagramAccount
from app.models.reel import Reel
from app.schemas.account import AccountCreate, AccountResponse, AccountUpdate
from app.schemas.reel import ReelResponse

router = APIRouter()


def _serialize_account(acc: InstagramAccount, reels_count: int = 0) -> dict:
    return {
        "id": acc.id,
        "instagram_username": acc.instagram_username,
        "instagram_user_id": acc.instagram_user_id,
        "full_name": acc.full_name,
        "profile_pic_url": acc.profile_pic_url,
        "bio": acc.bio,
        "followers_count": acc.followers_count,
        "following_count": acc.following_count,
        "posts_count": acc.posts_count,
        "sync_enabled": acc.sync_enabled,
        "last_synced_at": acc.last_synced_at,
        "last_sync_error": acc.last_sync_error,
        "auto_download_media": acc.auto_download_media,
        "reels_count": reels_count,
        "created_at": acc.created_at,
    }


@router.get("", response_model=List[AccountResponse])
def list_accounts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Список Instagram-аккаунтов юзера с количеством рилсов"""
    accs = db.query(InstagramAccount).filter(InstagramAccount.user_id == current_user.id).order_by(InstagramAccount.created_at.desc()).all()
    result = []
    for acc in accs:
        count = db.query(Reel).filter(Reel.instagram_account_id == acc.id).count()
        result.append(_serialize_account(acc, count))
    return result


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
def add_account(
    data: AccountCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Добавить Instagram-аккаунт (достанет профиль, запустит первый sync)"""
    username = data.username.strip().lstrip('@').lower()
    if not username:
        raise HTTPException(400, detail="Пустой username")

    # Проверим дубль
    existing = db.query(InstagramAccount).filter(
        InstagramAccount.user_id == current_user.id,
        InstagramAccount.instagram_username == username,
    ).first()
    if existing:
        raise HTTPException(400, detail=f"Аккаунт @{username} уже добавлен")

    # Получаем метаданные профиля прямо сейчас (быстрая операция)
    from app.workers.parser_worker import get_parser
    parser = get_parser()
    profile = parser.fetch_instagram_profile(username)
    if not profile:
        raise HTTPException(404, detail=f"Не удалось получить профиль @{username}. Проверь username.")

    acc = InstagramAccount(
        user_id=current_user.id,
        instagram_username=profile.get('username') or username,
        instagram_user_id=profile.get('instagram_user_id'),
        full_name=profile.get('full_name'),
        profile_pic_url=profile.get('profile_pic_url'),
        bio=profile.get('bio'),
        followers_count=profile.get('followers_count'),
        following_count=profile.get('following_count'),
        posts_count=profile.get('posts_count'),
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)

    # Ставим в очередь синхронизацию рилсов (в фоне)
    from app.services.parsing_service import create_account_sync_job
    try:
        create_account_sync_job(db, current_user, acc)
    except Exception:
        pass  # не критично — пользователь сможет нажать «Обновить»

    return _serialize_account(acc, 0)


@router.get("/{account_id}", response_model=AccountResponse)
def get_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    acc = db.query(InstagramAccount).filter(
        InstagramAccount.id == account_id,
        InstagramAccount.user_id == current_user.id,
    ).first()
    if not acc:
        raise HTTPException(404, detail="Аккаунт не найден")
    count = db.query(Reel).filter(Reel.instagram_account_id == acc.id).count()
    return _serialize_account(acc, count)


@router.get("/{account_id}/reels", response_model=List[ReelResponse])
def account_reels(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Рилсы аккаунта, отсортированные по position_in_account (1 = самый свежий)"""
    acc = db.query(InstagramAccount).filter(
        InstagramAccount.id == account_id,
        InstagramAccount.user_id == current_user.id,
    ).first()
    if not acc:
        raise HTTPException(404, detail="Аккаунт не найден")
    return (
        db.query(Reel)
        .filter(Reel.instagram_account_id == acc.id)
        .order_by(Reel.position_in_account.asc().nullslast())
        .all()
    )


@router.post("/{account_id}/sync")
def sync_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Запустить синхронизацию рилсов аккаунта"""
    acc = db.query(InstagramAccount).filter(
        InstagramAccount.id == account_id,
        InstagramAccount.user_id == current_user.id,
    ).first()
    if not acc:
        raise HTTPException(404, detail="Аккаунт не найден")
    from app.services.parsing_service import create_account_sync_job
    create_account_sync_job(db, current_user, acc)
    return {"status": "queued", "account_id": account_id}


@router.patch("/{account_id}", response_model=AccountResponse)
def update_account(
    account_id: int,
    data: AccountUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Обновить флаги аккаунта (`sync_enabled`, `auto_download_media`)."""
    acc = db.query(InstagramAccount).filter(
        InstagramAccount.id == account_id,
        InstagramAccount.user_id == current_user.id,
    ).first()
    if not acc:
        raise HTTPException(404, detail="Аккаунт не найден")
    if data.sync_enabled is not None:
        acc.sync_enabled = data.sync_enabled
    if data.auto_download_media is not None:
        acc.auto_download_media = data.auto_download_media
    if data.auto_analyze_media is not None:
        acc.auto_analyze_media = data.auto_analyze_media
    if data.auto_remake_enabled is not None:
        acc.auto_remake_enabled = data.auto_remake_enabled
    if data.auto_uniqify is not None:
        acc.auto_uniqify = data.auto_uniqify
    if data.auto_publish is not None:
        acc.auto_publish = data.auto_publish
    if data.viral_growth_threshold is not None:
        acc.viral_growth_threshold = data.viral_growth_threshold
    if data.viral_window_hours is not None:
        acc.viral_window_hours = data.viral_window_hours
    if data.default_remake_params is not None:
        acc.default_remake_params = data.default_remake_params
    if data.auto_posting_target_id is not None:
        acc.auto_posting_target_id = data.auto_posting_target_id
    db.commit()
    db.refresh(acc)
    count = db.query(Reel).filter(Reel.instagram_account_id == acc.id).count()
    return _serialize_account(acc, count)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удалить аккаунт (рилсы, привязанные к нему, открепятся, но останутся)"""
    acc = db.query(InstagramAccount).filter(
        InstagramAccount.id == account_id,
        InstagramAccount.user_id == current_user.id,
    ).first()
    if not acc:
        raise HTTPException(404, detail="Аккаунт не найден")
    db.delete(acc)
    db.commit()
