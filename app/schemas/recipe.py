"""
Pydantic схемы для ContentRecipe API.
"""

from datetime import datetime
from typing import Any, List, Optional
from pydantic import BaseModel, Field


class RecipeResponse(BaseModel):
    id: int
    source_reel_id: Optional[int] = None
    name: str
    hook_type: Optional[str] = None
    duration_sec: Optional[int] = None
    language: Optional[str] = None
    hook: Optional[dict[str, Any]] = None
    structure: Optional[List[dict[str, Any]]] = None
    visual_motifs: Optional[List[str]] = None
    audio_strategy: Optional[dict[str, Any]] = None
    cta: Optional[dict[str, Any]] = None
    canonical_prompt: Optional[str] = None
    extractor_model: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RecipeListResponse(BaseModel):
    items: List[RecipeResponse]
    total: int
