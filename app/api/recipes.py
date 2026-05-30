"""
API для ContentRecipe — извлечение / список / детали / удаление.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.services.reel_service import get_reel_by_id
from app.services.recipe_service import (
    extract_recipe_from_reel,
    list_user_recipes,
    get_recipe_by_id,
    delete_recipe,
)
from app.core.recipe_extractor import RecipeExtractError
from app.schemas.recipe import RecipeResponse, RecipeListResponse

router = APIRouter()


@router.post(
    "/from-reel/{reel_id}",
    response_model=RecipeResponse,
    status_code=status.HTTP_201_CREATED,
)
def extract_from_reel(
    reel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Извлечь ContentRecipe из проанализированного рилса.

    Pre-requisites:
    - reel должен быть проанализирован (transcript / visual_summary не NULL)
    - user.openai_api_key установлен

    Синхронно — extractor делает 1 LLM call (~5-10 сек).
    """
    reel = get_reel_by_id(db, reel_id, current_user)
    try:
        recipe = extract_recipe_from_reel(db, current_user, reel)
    except RecipeExtractError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return recipe


@router.get("", response_model=RecipeListResponse)
def list_recipes(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items, total = list_user_recipes(db, current_user, limit=limit, offset=offset)
    return RecipeListResponse(items=items, total=total)


@router.get("/{recipe_id}", response_model=RecipeResponse)
def get_recipe(
    recipe_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    r = get_recipe_by_id(db, recipe_id, current_user)
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Recipe #{recipe_id} не найден")
    return r


@router.delete("/{recipe_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_recipe(
    recipe_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    r = get_recipe_by_id(db, recipe_id, current_user)
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Recipe #{recipe_id} не найден")
    delete_recipe(db, r)
