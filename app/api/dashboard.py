"""
API дашборда: агрегированная статистика
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.reel import Reel

router = APIRouter()


@router.get("")
def get_dashboard(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Агрегированная статистика по всем рилсам юзера"""

    stats = db.query(
        func.count(Reel.id).label("total_reels"),
        func.coalesce(func.sum(Reel.views), 0).label("total_views"),
        func.coalesce(func.sum(Reel.likes), 0).label("total_likes"),
        func.coalesce(func.sum(Reel.comments), 0).label("total_comments"),
        func.coalesce(func.sum(Reel.shares), 0).label("total_shares"),
    ).filter(Reel.user_id == current_user.id).first()

    # Средний ER
    total_interactions = stats.total_likes + stats.total_comments + stats.total_shares
    avg_er = 0
    if stats.total_views > 0:
        avg_er = round((total_interactions / stats.total_views) * 100, 2)

    # Топ рилс по просмотрам
    top_reel = db.query(Reel).filter(
        Reel.user_id == current_user.id,
    ).order_by(Reel.views.desc()).first()

    return {
        "total_reels": stats.total_reels,
        "total_views": stats.total_views,
        "total_likes": stats.total_likes,
        "total_comments": stats.total_comments,
        "total_shares": stats.total_shares,
        "avg_er": avg_er,
        "top_reel": {
            "id": top_reel.id,
            "title": top_reel.title,
            "views": top_reel.views,
        } if top_reel else None,
        "tariff": current_user.tariff.value,
    }
