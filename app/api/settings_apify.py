"""
API для настройки Apify API token
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User

router = APIRouter()


class ApifyTokenPayload(BaseModel):
    token: str


@router.get("")
def get_apify_settings(current_user: User = Depends(get_current_user)):
    """Вернёт есть ли токен и маскированную превью"""
    token = current_user.apify_token or ''
    masked = ''
    if token:
        masked = token[:8] + '…' + token[-4:] if len(token) > 16 else '***'
    return {"configured": bool(token), "preview": masked}


@router.put("")
def save_apify_token(
    data: ApifyTokenPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Сохранить Apify API token"""
    token = (data.token or '').strip()
    if token and not token.startswith('apify_api_'):
        raise HTTPException(400, detail="Токен должен начинаться с 'apify_api_'")
    current_user.apify_token = token or None
    db.commit()
    return {"status": "ok", "configured": bool(current_user.apify_token)}


@router.delete("")
def clear_apify_token(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current_user.apify_token = None
    db.commit()
    return {"status": "ok"}
