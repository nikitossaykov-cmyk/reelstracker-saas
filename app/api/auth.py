"""
API авторизации: регистрация, логин, обновление токена, профиль
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.user import UserCreate, UserLogin, UserResponse, Token, TokenRefresh
from app.services.auth_service import (
    get_user_by_email,
    create_user,
    authenticate_user,
    create_access_token,
    create_refresh_token,
    decode_token,
)

router = APIRouter()


def _make_tokens(user: User) -> Token:
    """Сгенерировать пару токенов для юзера"""
    access = create_access_token(data={"sub": str(user.id)})
    refresh = create_refresh_token(data={"sub": str(user.id)})
    reels_count = len(user.reels) if user.reels else 0
    return Token(
        access_token=access,
        refresh_token=refresh,
        user=UserResponse(
            id=user.id,
            email=user.email,
            tariff=user.tariff.value,
            is_active=user.is_active,
            created_at=user.created_at,
            reels_count=reels_count,
        ),
    )


@router.post("/register", response_model=Token, status_code=status.HTTP_201_CREATED)
def register(data: UserCreate, db: Session = Depends(get_db)):
    """Регистрация нового пользователя"""

    # Проверяем, не занят ли email
    if get_user_by_email(db, data.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Пользователь с таким email уже существует",
        )

    user = create_user(db, data.email, data.password)
    return _make_tokens(user)


@router.post("/login", response_model=Token)
def login(data: UserLogin, db: Session = Depends(get_db)):
    """Вход по email + пароль"""

    user = authenticate_user(db, data.email, data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный email или пароль",
        )

    return _make_tokens(user)


@router.post("/refresh", response_model=Token)
def refresh_token(data: TokenRefresh, db: Session = Depends(get_db)):
    """Обновление access токена по refresh токену"""

    payload = decode_token(data.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Невалидный refresh token",
        )

    user_id = int(payload.get("sub"))
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Пользователь не найден или деактивирован",
        )

    return _make_tokens(user)


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """Получить профиль текущего юзера"""
    reels_count = len(current_user.reels) if current_user.reels else 0
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        tariff=current_user.tariff.value,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
        reels_count=reels_count,
    )


class ApiKeysUpdate(BaseModel):
    openai_api_key: Optional[str] = Field(None, max_length=255)
    runway_api_key: Optional[str] = Field(None, max_length=255)
    replicate_api_key: Optional[str] = Field(None, max_length=255)


class ApiKeysStatus(BaseModel):
    openai_set: bool
    runway_set: bool
    replicate_set: bool
    openai_preview: Optional[str] = None  # first 8 + last 4 chars only
    runway_preview: Optional[str] = None
    replicate_preview: Optional[str] = None


@router.get("/me/keys", response_model=ApiKeysStatus)
def get_my_keys(current_user: User = Depends(get_current_user)):
    def _preview(k: Optional[str]) -> Optional[str]:
        if not k:
            return None
        if len(k) <= 14:
            return k[:4] + "…"
        return f"{k[:8]}…{k[-4:]}"
    return ApiKeysStatus(
        openai_set=bool(current_user.openai_api_key),
        runway_set=bool(current_user.runway_api_key),
        replicate_set=bool(current_user.replicate_api_key),
        openai_preview=_preview(current_user.openai_api_key),
        runway_preview=_preview(current_user.runway_api_key),
        replicate_preview=_preview(current_user.replicate_api_key),
    )


@router.patch("/me/keys", response_model=ApiKeysStatus)
def update_my_keys(
    data: ApiKeysUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Сохранить OpenAI / Runway / Replicate API-ключи в профиль.
    Пустая строка ('') означает «оставить как есть»; чтобы стереть —
    отправляй null."""
    if data.openai_api_key is not None:
        current_user.openai_api_key = data.openai_api_key.strip() or None
    if data.runway_api_key is not None:
        current_user.runway_api_key = data.runway_api_key.strip() or None
    if data.replicate_api_key is not None:
        current_user.replicate_api_key = data.replicate_api_key.strip() or None
    db.commit()
    db.refresh(current_user)
    return get_my_keys(current_user)
