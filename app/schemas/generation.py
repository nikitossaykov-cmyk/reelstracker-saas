"""
Pydantic-схемы для generation API.
"""

from datetime import datetime
from typing import Optional, List, Literal
from pydantic import BaseModel, Field

from app.models.generation import (
    GenerationStatus,
    VideoProvider,
)


class GenerationCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    provider: VideoProvider = VideoProvider.RUNWAY
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    duration_seconds: int = Field(5, ge=1, le=30)
    init_image_url: Optional[str] = Field(None, max_length=1024)
    seed: Optional[int] = None
    # Runway-specific: 'gen4.5' (default) / 'veo3.1_fast' / 'gen4_turbo'
    model: Optional[str] = Field(None, max_length=64)


class GeneratedVideoResponse(BaseModel):
    id: int
    prompt: str
    provider: VideoProvider
    status: GenerationStatus
    provider_job_id: Optional[str] = None
    cost_kopecks: Optional[int] = None
    media_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class GenerationListResponse(BaseModel):
    items: List[GeneratedVideoResponse]
    total: int
