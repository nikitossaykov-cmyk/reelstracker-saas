"""
API для генерации видео: POST /api/generation, GET список + детальная.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.generation import VideoProvider
from app.schemas.generation import (
    GenerationCreate,
    GeneratedVideoResponse,
    GenerationListResponse,
)
from app.services.generation_service import (
    create_generation_job,
    get_user_generations,
    get_generation_by_id,
)

router = APIRouter()


@router.post("", response_model=GeneratedVideoResponse, status_code=status.HTTP_201_CREATED)
def create_generation(
    data: GenerationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Поставить генерацию видео в очередь.

    Provider Runway требует наличия user.runway_api_key — иначе worker
    зафейлит задачу с понятной ошибкой. Чтобы не давать юзеру ставить
    заведомо-провальные задачи, проверяем ключ заранее.
    """
    if data.provider == VideoProvider.RUNWAY and not current_user.runway_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Для провайдера 'runway' нужно сначала сохранить runway_api_key "
                   "в настройках профиля.",
        )
    gv = create_generation_job(
        db=db,
        user=current_user,
        prompt=data.prompt,
        provider=data.provider,
        aspect_ratio=data.aspect_ratio,
        duration_seconds=data.duration_seconds,
        init_image_url=data.init_image_url,
        seed=data.seed,
        model=data.model,
    )
    return gv


@router.get("", response_model=GenerationListResponse)
def list_generations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Список генераций юзера, новые сверху."""
    items, total = get_user_generations(db, current_user, limit=limit, offset=offset)
    return GenerationListResponse(items=items, total=total)


@router.get("/{generation_id}", response_model=GeneratedVideoResponse)
def get_generation(
    generation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Детальная информация об одной генерации (для polling из UI)."""
    gv = get_generation_by_id(db, generation_id, current_user)
    if gv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Generation #{generation_id} не найдена",
        )
    return gv
