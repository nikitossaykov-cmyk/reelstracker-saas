"""
Сервис ContentRecipe — извлечение из проанализированного рилса
+ CRUD.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.user import User
from app.models.reel import Reel
from app.models.recipe import ContentRecipe
from app.core.recipe_extractor import extract_recipe, RecipeExtractError

logger = logging.getLogger(__name__)


def extract_recipe_from_reel(
    db: Session,
    user: User,
    reel: Reel,
    openai_api_key: Optional[str] = None,
) -> ContentRecipe:
    """Запустить extractor синхронно (один LLM call ~5-10 сек) и сохранить."""
    api_key = openai_api_key or user.openai_api_key
    if not api_key:
        raise RecipeExtractError("user.openai_api_key пуст")
    if not (reel.transcript or reel.visual_summary):
        raise RecipeExtractError("Reel не проанализирован (нет transcript/visual_summary) "
                                 "— сначала POST /api/reels/{id}/analyze")

    scenes_list = None
    if reel.scenes:
        try:
            scenes_list = json.loads(reel.scenes)
        except json.JSONDecodeError:
            scenes_list = None

    parsed, raw = extract_recipe(
        transcript=reel.transcript,
        visual_summary=reel.visual_summary,
        scenes=scenes_list,
        hook_type=reel.hook_type,
        duration_sec=reel.duration_seconds,
        openai_api_key=api_key,
    )

    recipe = ContentRecipe(
        user_id=user.id,
        source_reel_id=reel.id,
        name=(parsed.get("name") or f"Recipe from reel #{reel.id}")[:255],
        hook_type=(parsed.get("hook_type") or reel.hook_type or "OTHER")[:64],
        duration_sec=int(parsed.get("duration_sec") or (reel.duration_seconds or 0)) or None,
        language=(parsed.get("language") or None),
        hook=parsed.get("hook"),
        structure=parsed.get("structure"),
        visual_motifs=parsed.get("visual_motifs"),
        audio_strategy=parsed.get("audio_strategy"),
        cta=parsed.get("cta"),
        canonical_prompt=parsed.get("canonical_prompt"),
        raw_extractor_response=raw,
        extractor_model="gpt-4o-mini",
    )
    db.add(recipe)
    db.commit()
    db.refresh(recipe)
    logger.info(f"✅ ContentRecipe #{recipe.id} from reel #{reel.id}")
    return recipe


def list_user_recipes(
    db: Session, user: User, limit: int = 50, offset: int = 0,
) -> tuple[list[ContentRecipe], int]:
    q = db.query(ContentRecipe).filter(ContentRecipe.user_id == user.id)
    total = q.with_entities(func.count(ContentRecipe.id)).scalar() or 0
    items = (q.order_by(ContentRecipe.created_at.desc())
             .limit(limit).offset(offset).all())
    return items, total


def get_recipe_by_id(
    db: Session, recipe_id: int, user: User,
) -> Optional[ContentRecipe]:
    return db.query(ContentRecipe).filter(
        ContentRecipe.id == recipe_id,
        ContentRecipe.user_id == user.id,
    ).first()


def delete_recipe(db: Session, recipe: ContentRecipe) -> None:
    db.delete(recipe)
    db.commit()
