"""
Pydantic схемы для пользователей и авторизации
"""

from pydantic import BaseModel, EmailStr, Field
from datetime import datetime
from typing import Optional


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    tariff: str
    is_active: bool
    created_at: datetime
    reels_count: Optional[int] = 0

    model_config = {"from_attributes": True}


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse


class TokenRefresh(BaseModel):
    refresh_token: str
