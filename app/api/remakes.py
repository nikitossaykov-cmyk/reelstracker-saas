"""
API ремейка — берёт recipe (или сырой reel) + параметры подмены →
ставит GENERATE_VIDEO задачу с rendered prompt.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.generation import VideoProvider
from app.services.recipe_service import get_recipe_by_id
from app.services.reel_service import get_reel_by_id
from app.services.remake_service import create_remake_job
from app.core.composer import RemakeParams
from app.schemas.remake import RemakeFromRecipeRequest, RemakeFromReelRequest
from app.schemas.generation import GeneratedVideoResponse

router = APIRouter()


def _params_from_schema(p) -> RemakeParams:
    return RemakeParams(
        brand=p.brand,
        product_description=p.product_description,
        face_description=p.face_description,
        voice_description=p.voice_description,
        location_description=p.location_description,
        outfit_description=p.outfit_description,
        palette=p.palette,
        extra_instructions=p.extra_instructions,
    )


def _check_provider_key(user: User, provider: VideoProvider) -> None:
    if provider == VideoProvider.RUNWAY and not user.runway_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Для провайдера 'runway' нужен user.runway_api_key.",
        )


@router.post("/from-recipe", response_model=GeneratedVideoResponse, status_code=status.HTTP_201_CREATED)
def remake_from_recipe(
    data: RemakeFromRecipeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Создать ремейк по существующему ContentRecipe."""
    recipe = get_recipe_by_id(db, data.recipe_id, current_user)
    if not recipe:
        raise HTTPException(404, detail=f"Recipe #{data.recipe_id} не найден")
    _check_provider_key(current_user, data.provider)
    return create_remake_job(
        db, current_user,
        recipe=recipe,
        params=_params_from_schema(data.params),
        provider=data.provider,
        aspect_ratio=data.aspect_ratio,
        duration_seconds=data.duration_seconds,
        model=data.model,
    )


@router.post("/from-reel", response_model=GeneratedVideoResponse, status_code=status.HTTP_201_CREATED)
def remake_from_reel(
    data: RemakeFromReelRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Создать ремейк прямо из reel — пропускаем шаг recipe.

    Полезно для быстрого one-shot: видишь reel → жмёшь «сделай как этот»,
    используется visual_summary + параметры подмены.
    """
    reel = get_reel_by_id(db, data.reel_id, current_user)
    _check_provider_key(current_user, data.provider)
    return create_remake_job(
        db, current_user,
        source_reel=reel,
        params=_params_from_schema(data.params),
        provider=data.provider,
        aspect_ratio=data.aspect_ratio,
        duration_seconds=data.duration_seconds,
        model=data.model,
    )
