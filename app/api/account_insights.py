"""
Account Insights API — анализ всего аккаунта конкурента одним вызовом.

Цель: юзер видит «вот этот аккаунт — TWIST-формат, перфюм-ниша,
средние просмотры 200k, рекомендую ремейкать видео X и Y». Дальше
кликает по конкретному рилсу → попадает в Magic Mode со ссылкой.
"""

from typing import Optional, List
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_user
from app.models.user import User

router = APIRouter()


class AccountAnalyzeRequest(BaseModel):
    account: str = Field(min_length=1, max_length=512, description="@username, username, или полная URL")
    max_reels: int = Field(10, ge=3, le=30)


class ReelCompact(BaseModel):
    url: Optional[str] = None
    title: Optional[str] = None
    duration: Optional[float] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    thumbnail: Optional[str] = None
    upload_date: Optional[str] = None


class AccountInsightResponse(BaseModel):
    account_url: str
    reels: List[ReelCompact]
    insights: dict


@router.post("/analyze", response_model=AccountInsightResponse)
def analyze_account_endpoint(
    data: AccountAnalyzeRequest,
    current_user: User = Depends(get_current_user),
):
    """Анализ аккаунта: скачивает metadata топ-N рилсов через yt-dlp,
    LLM-агрегирует → структурный summary. ~$0.005-0.01 за анализ.

    Без скачивания каждого видео — только metadata. Для детального
    разбора конкретного рилса используй /api/magic/from-url по его URL.
    """
    if not current_user.openai_api_key:
        raise HTTPException(400, detail="Нужен openai_api_key в профиле")
    from app.core.account_analyzer import analyze_account, AccountAnalyzeError
    try:
        result = analyze_account(
            data.account,
            openai_api_key=current_user.openai_api_key,
            max_reels=data.max_reels,
        )
    except AccountAnalyzeError as e:
        raise HTTPException(502, detail=f"account analyze: {e}")
    return AccountInsightResponse(**result)
