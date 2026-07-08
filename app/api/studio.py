"""REST API for the single-take UGC Studio (POC).

  GET  /api/studio/jobs/            — list current user's jobs
  POST /api/studio/jobs/            — multipart form → enqueue (202)
  GET  /api/studio/jobs/{jid}       — poll one job
  POST /api/studio/jobs/{jid}/retry — reset FAILED job to PENDING (full restart)
  POST /api/studio/script           — script autogen for the textarea
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, UploadFile, status,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.studio_job import StudioJob, StudioStatus
from app.models.user import User
from app.services.strategy_makeugc.script import resolve_openai_key
from app.services.strategy_single_take.script import generate_studio_script
from app.services.studio_service import (
    StudioValidationError,
    create_studio_job_async,
)


router = APIRouter()


class StudioJobResponse(BaseModel):
    id: int
    product_name: str
    brand: str
    price_rub: float
    dupe_price_rub: float
    voice_style: str
    captions_enabled: bool
    cutaways_enabled: bool
    script_text: Optional[str]
    hook_video_key: Optional[str]
    status: str
    portrait_key: Optional[str]
    voiceover_key: Optional[str]
    lipsync_key: Optional[str]
    cap_still_key: Optional[str]
    spray_still_key: Optional[str]
    cap_clip_key: Optional[str]
    spray_clip_key: Optional[str]
    output_key: Optional[str]
    judge_score: Optional[int]
    judge_report: Optional[dict]
    error_message: Optional[str]
    cost_usd: float
    created_at: str
    completed_at: Optional[str]

    @classmethod
    def from_model(cls, j: StudioJob) -> "StudioJobResponse":
        return cls(
            id=j.id,
            product_name=j.product_name,
            brand=j.brand,
            price_rub=float(j.price_rub),
            dupe_price_rub=float(j.dupe_price_rub),
            voice_style=j.voice_style,
            captions_enabled=bool(j.captions_enabled),
            cutaways_enabled=bool(j.cutaways_enabled),
            script_text=j.script_text,
            hook_video_key=j.hook_video_key,
            status=j.status,
            portrait_key=j.portrait_key,
            voiceover_key=j.voiceover_key,
            lipsync_key=j.lipsync_key,
            cap_still_key=j.cap_still_key,
            spray_still_key=j.spray_still_key,
            cap_clip_key=j.cap_clip_key,
            spray_clip_key=j.spray_clip_key,
            output_key=j.output_key,
            judge_score=j.judge_score,
            judge_report=j.judge_report,
            error_message=j.error_message,
            cost_usd=float(j.cost_usd or 0),
            created_at=j.created_at.isoformat(),
            completed_at=j.completed_at.isoformat() if j.completed_at else None,
        )


class StudioJobListResponse(BaseModel):
    items: list[StudioJobResponse]


class ScriptRequest(BaseModel):
    product_name: str
    brand: str
    price_rub: float
    dupe_price_rub: float
    voice_style: str = "normal"
    cutaways_enabled: bool = True


def _get_owned(db: Session, user: User, jid: int) -> StudioJob:
    j = (
        db.query(StudioJob)
        .filter(StudioJob.id == jid, StudioJob.user_id == user.id)
        .first()
    )
    if not j:
        raise HTTPException(404, "studio job not found")
    return j


@router.get("/jobs/", response_model=StudioJobListResponse)
def list_jobs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(StudioJob)
        .filter(StudioJob.user_id == current_user.id)
        .order_by(StudioJob.created_at.desc())
        .all()
    )
    return StudioJobListResponse(
        items=[StudioJobResponse.from_model(r) for r in rows]
    )


@router.post(
    "/jobs/",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StudioJobResponse,
)
async def create_job(
    product_images: list[UploadFile] = File(...),
    product_name: str = Form(...),
    brand: str = Form(...),
    price_rub: str = Form(...),
    dupe_price_rub: str = Form(...),
    script_text: Optional[str] = Form(None),
    voice_style: str = Form("normal"),
    captions_enabled: bool = Form(True),
    cutaways_enabled: bool = Form(True),
    hook_video: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        price_dec = Decimal(price_rub)
        dupe_dec = Decimal(dupe_price_rub)
    except InvalidOperation:
        raise HTTPException(400, "price_rub and dupe_price_rub must be decimals")

    images: list[tuple[bytes, str]] = []
    for f in product_images:
        blob = await f.read()
        images.append((blob, f.content_type or "application/octet-stream"))

    hook_pair: tuple[bytes, str] | None = None
    if hook_video is not None and hook_video.filename:
        hook_blob = await hook_video.read()
        if hook_blob:
            hook_pair = (
                hook_blob, hook_video.content_type or "application/octet-stream",
            )

    try:
        j = create_studio_job_async(
            db, current_user,
            product_images=images,
            product_name=product_name,
            brand=brand,
            price_rub=price_dec,
            dupe_price_rub=dupe_dec,
            script_text=script_text,
            voice_style=voice_style,
            captions_enabled=captions_enabled,
            cutaways_enabled=cutaways_enabled,
            hook_video=hook_pair,
        )
    except StudioValidationError as e:
        raise HTTPException(400, str(e))
    return StudioJobResponse.from_model(j)


@router.get("/jobs/{jid}", response_model=StudioJobResponse)
def get_job(
    jid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return StudioJobResponse.from_model(_get_owned(db, current_user, jid))


@router.post("/jobs/{jid}/retry", response_model=StudioJobResponse)
def retry_job(
    jid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    j = _get_owned(db, current_user, jid)
    if j.status not in (StudioStatus.FAILED, StudioStatus.READY):
        raise HTTPException(409, f"job is {j.status}, can't retry")
    # POC: full pipeline restart — clear all stage artifacts.
    # (resume-from-stage is a spec'd follow-up; cost_usd keeps accruing
    # because the money was actually spent)
    j.status = StudioStatus.PENDING
    j.error_message = None
    j.portrait_key = None
    j.voiceover_key = None
    j.lipsync_key = None
    j.cap_still_key = None
    j.spray_still_key = None
    j.cap_clip_key = None
    j.spray_clip_key = None
    j.output_key = None
    j.judge_score = None
    j.judge_report = None
    j.completed_at = None
    db.commit()
    db.refresh(j)
    return StudioJobResponse.from_model(j)


@router.post("/script")
def make_script(
    req: ScriptRequest,
    current_user: User = Depends(get_current_user),
):
    openai_key = resolve_openai_key(current_user.openai_api_key)
    if not openai_key:
        raise HTTPException(400, "Нет OPENAI_API_KEY (ни у юзера, ни в env)")
    try:
        text = generate_studio_script(
            product_name=req.product_name,
            brand=req.brand,
            price_rub=req.price_rub,
            dupe_price_rub=req.dupe_price_rub,
            voice_style=req.voice_style,
            cutaways=req.cutaways_enabled,
            openai_api_key=openai_key,
        )
    except Exception as e:
        raise HTTPException(502, f"script-gen ошибка: {str(e)[:200]}")
    return {"script_text": text}
