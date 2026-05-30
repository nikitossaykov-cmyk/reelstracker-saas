"""
Pydantic схемы для Remake API (поверх generation).
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field

from app.models.generation import VideoProvider


class RemakeParamsSchema(BaseModel):
    """Поля для подмены плейсхолдеров в recipe.canonical_prompt."""
    brand: Optional[str] = Field(None, max_length=255)
    product_description: Optional[str] = Field(None, max_length=1000)
    face_description: Optional[str] = Field(None, max_length=1000)
    voice_description: Optional[str] = Field(None, max_length=1000)
    location_description: Optional[str] = Field(None, max_length=1000)
    outfit_description: Optional[str] = Field(None, max_length=1000)
    palette: Optional[str] = Field(None, max_length=500)
    extra_instructions: Optional[str] = Field(None, max_length=2000)


class RemakeFromRecipeRequest(BaseModel):
    recipe_id: int
    params: RemakeParamsSchema = RemakeParamsSchema()
    provider: VideoProvider = VideoProvider.RUNWAY
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    duration_seconds: int = Field(5, ge=1, le=30)
    model: Optional[str] = Field(None, max_length=64)


class RemakeFromReelRequest(BaseModel):
    reel_id: int
    params: RemakeParamsSchema = RemakeParamsSchema()
    provider: VideoProvider = VideoProvider.RUNWAY
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    duration_seconds: int = Field(5, ge=1, le=30)
    model: Optional[str] = Field(None, max_length=64)
