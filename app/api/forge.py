"""
Unified Forge API — single entrypoint that routes to one of three
remake strategies:

  A = full LLM remake (current Magic Mode):
      transcript + vision + recipe + Runway image-to-video (per scene)
      cost: ~$0.10-0.30, time: 3-7 min

  B = uniqualizer (anti-fingerprint over original):
      ffmpeg video mutations + audio shift + subtitle rewrite
      cost: ~$0.005, time: ~30 sec, NOT a "new video" — same content
      with platform-fooling perturbations

  C = frame-level edit via image inpaint (MVP):
      sample N keyframes → GPT-image-1 / FLUX inpaint with brand/face
      override → re-stitch. Preserves timing exactly (no subtitle drift).
      cost: ~$0.50-2, time: ~5-10 min
"""
from __future__ import annotations

from typing import Optional, Literal
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.generation import VideoProvider

router = APIRouter()


Strategy = Literal["A", "B", "C"]


class ForgeStartRequest(BaseModel):
    strategy: Strategy = Field(description="A=Magic, B=Uniqualizer, C=Frame-edit")
    source_url: str = Field(min_length=10, max_length=2048)
    # Common
    brand: Optional[str] = Field(None, max_length=255)
    product_description: Optional[str] = Field(None, max_length=1000)
    extra_instructions: Optional[str] = Field(None, max_length=2000)
    # A-specific (Magic / Runway)
    provider: VideoProvider = VideoProvider.RUNWAY
    model: Optional[str] = Field(None, max_length=64)
    duration_seconds: int = Field(5, ge=1, le=30)
    # B-specific (uniqualizer toggles) — placeholder, PR-2 wires them up
    b_mutate_video: bool = True
    b_mutate_audio: bool = True
    b_rewrite_subs: bool = True
    # C-specific (frame edit)
    c_keyframe_count: int = Field(5, ge=3, le=30)
    c_smooth_transitions: bool = False  # PR-6: Runway image_to_video between keyframes


class ForgeStartResponse(BaseModel):
    strategy: Strategy
    reel_id: Optional[int] = None
    magic_account_id: Optional[int] = None
    job_id: Optional[int] = None
    gv_id: Optional[int] = None
    media_url: Optional[str] = None  # B returns it directly (sync); A polls
    source_title: Optional[str] = None
    next_step: str
    cost_estimate_usd: float
    cost_actual_usd: Optional[float] = None  # B fills this; A/C estimate-only


def _estimate_cost(strategy: Strategy, duration_seconds: int, model: Optional[str],
                   c_keyframe_count: int = 5, c_smooth: bool = False) -> float:
    if strategy == "A":
        # Runway gen4.5: ~$0.05/sec, assume 4 scenes × duration sec each
        per_sec = {"gen4.5": 0.05, "gen4_turbo": 0.05, "veo3.1_fast": 0.40}.get(
            (model or "gen4.5"), 0.05,
        )
        return round(4 * duration_seconds * per_sec + 0.01, 3)  # +OpenAI analyzer
    if strategy == "B":
        return 0.01  # Whisper + LLM rewrite only
    if strategy == "C":
        base = 0.04 * c_keyframe_count + 0.01  # gpt-image-1 inpaint
        if c_smooth:
            # (N-1) runway segments × ≥5s × $0.05/s
            base += (c_keyframe_count - 1) * 5 * 0.05
        return round(base, 3)
    return 0.0


@router.post("/start", response_model=ForgeStartResponse, status_code=status.HTTP_202_ACCEPTED)
def forge_start(
    data: ForgeStartRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Single entrypoint for all 3 remake strategies. UI sends `strategy=A|B|C`."""
    cost = _estimate_cost(
        data.strategy, data.duration_seconds, data.model,
        c_keyframe_count=data.c_keyframe_count,
        c_smooth=data.c_smooth_transitions,
    )

    if data.strategy == "A":
        if not current_user.runway_api_key:
            raise HTTPException(400, detail="Нужен runway_api_key в профиле")
        if not current_user.openai_api_key:
            raise HTTPException(400, detail="Нужен openai_api_key в профиле")
        from app.services.magic_service import start_magic_from_url
        try:
            result = start_magic_from_url(
                db, current_user,
                source_url=data.source_url,
                brand=data.brand,
                product_description=data.product_description,
                extra_instructions=data.extra_instructions,
                provider=data.provider,
                model=data.model,
                duration_seconds=data.duration_seconds,
            )
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        return ForgeStartResponse(
            strategy="A",
            reel_id=result["reel_id"],
            magic_account_id=result["magic_account_id"],
            source_title=(result.get("source_meta") or {}).get("title"),
            next_step=f"GET /api/magic/{result['reel_id']}/status",
            cost_estimate_usd=cost,
        )

    if data.strategy == "B":
        from app.services.uniqualizer_b_service import run_strategy_b, StrategyBError
        try:
            result = run_strategy_b(
                db, current_user,
                source_url=data.source_url,
                mutate_video=data.b_mutate_video,
                mutate_audio=data.b_mutate_audio,
                rewrite_subs=data.b_rewrite_subs,
                brand=data.brand,
                product_description=data.product_description,
            )
        except StrategyBError as e:
            raise HTTPException(502, detail=f"strategy B: {e}")
        return ForgeStartResponse(
            strategy="B",
            gv_id=result["gv_id"],
            media_url=result["media_url"],
            source_title=result.get("source_title"),
            next_step="ready — media_url is the final video",
            cost_estimate_usd=cost,
            cost_actual_usd=result["cost_usd"],
        )

    if data.strategy == "C":
        # C / C+ takes 2-10 min — too long for sync HTTP under Railway's
        # ~5 min proxy timeout. Submit to background thread and return
        # gv_id immediately; UI polls /api/media/diag/{gv_id} for status.
        from app.services.frame_inpaint_c_service import StrategyCError, start_strategy_c_async
        try:
            gv_id = start_strategy_c_async(
                db, current_user,
                source_url=data.source_url,
                brand=data.brand,
                product_description=data.product_description,
                extra_instructions=data.extra_instructions,
                keyframe_count=data.c_keyframe_count,
                smooth_transitions=data.c_smooth_transitions,
            )
        except StrategyCError as e:
            raise HTTPException(502, detail=f"strategy C: {e}")
        return ForgeStartResponse(
            strategy="C",
            gv_id=gv_id,
            next_step=f"poll GET /api/media/diag/{gv_id} until status=ready",
            cost_estimate_usd=cost,
        )

    # unreachable — kept for safety
    if False and data.strategy == "C":
        from app.services.frame_inpaint_c_service import run_strategy_c, StrategyCError
        try:
            result = run_strategy_c(
                db, current_user,
                source_url=data.source_url,
                brand=data.brand,
                product_description=data.product_description,
                extra_instructions=data.extra_instructions,
                keyframe_count=data.c_keyframe_count,
                smooth_transitions=data.c_smooth_transitions,
            )
        except StrategyCError as e:
            raise HTTPException(502, detail=f"strategy C: {e}")
        return ForgeStartResponse(
            strategy="C",
            gv_id=result["gv_id"],
            media_url=result["media_url"],
            source_title=result.get("source_title"),
            next_step="ready — media_url is the final video",
            cost_estimate_usd=cost,
            cost_actual_usd=result["cost_usd"],
        )

    raise HTTPException(400, detail=f"unknown strategy: {data.strategy}")
