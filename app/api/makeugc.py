"""REST API for Strategy MakeUGC jobs.

Endpoints:
  GET  /api/makeugc/jobs/        — list current user's jobs
  POST /api/makeugc/jobs/        — multipart: product image + form fields → enqueue (202)
  GET  /api/makeugc/jobs/{jid}   — poll one job

All routes are user-scoped; cross-user access returns 404.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.makeugc_job import MakeUGCJob
from app.models.user import User
from app.services.makeugc_service import (
    MakeUGCValidationError,
    create_makeugc_job_async,
)


router = APIRouter()


class MakeUGCJobResponse(BaseModel):
    id: int
    product_name: str
    premium_brand: str
    premium_price_rub: float
    mimic_price_rub: float
    persona_style: str
    status: str
    portrait_key: Optional[str]
    voiceover_key: Optional[str]
    lipsync_key: Optional[str]
    bottle_hero_key: Optional[str]
    broll_video_key: Optional[str]
    output_key: Optional[str]
    script_text: Optional[str]
    error_message: Optional[str]
    cost_usd: float
    created_at: str
    completed_at: Optional[str]

    @classmethod
    def from_model(cls, j: MakeUGCJob) -> "MakeUGCJobResponse":
        return cls(
            id=j.id,
            product_name=j.product_name,
            premium_brand=j.premium_brand,
            premium_price_rub=float(j.premium_price_rub),
            mimic_price_rub=float(j.mimic_price_rub),
            persona_style=j.persona_style,
            status=j.status,
            portrait_key=j.portrait_key,
            voiceover_key=j.voiceover_key,
            lipsync_key=j.lipsync_key,
            bottle_hero_key=j.bottle_hero_key,
            broll_video_key=j.broll_video_key,
            output_key=j.output_key,
            script_text=j.script_text,
            error_message=j.error_message,
            cost_usd=float(j.cost_usd or 0),
            created_at=j.created_at.isoformat(),
            completed_at=j.completed_at.isoformat() if j.completed_at else None,
        )


class MakeUGCJobListResponse(BaseModel):
    items: list[MakeUGCJobResponse]


def _get_owned(db: Session, user: User, jid: int) -> MakeUGCJob:
    j = (
        db.query(MakeUGCJob)
        .filter(MakeUGCJob.id == jid, MakeUGCJob.user_id == user.id)
        .first()
    )
    if not j:
        raise HTTPException(404, "makeugc job not found")
    return j


@router.get("/jobs/", response_model=MakeUGCJobListResponse)
def list_jobs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(MakeUGCJob)
        .filter(MakeUGCJob.user_id == current_user.id)
        .order_by(MakeUGCJob.created_at.desc())
        .all()
    )
    return MakeUGCJobListResponse(
        items=[MakeUGCJobResponse.from_model(r) for r in rows]
    )


@router.post(
    "/jobs/",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MakeUGCJobResponse,
)
async def create_job(
    product_images: list[UploadFile] = File(...),
    product_name: str = Form(...),
    premium_brand: str = Form(...),
    premium_price_rub: str = Form(...),
    mimic_price_rub: str = Form(...),
    persona_style: str = Form("average-girl"),
    broll_video: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        premium_dec = Decimal(premium_price_rub)
        mimic_dec = Decimal(mimic_price_rub)
    except InvalidOperation:
        raise HTTPException(400, "premium_price_rub and mimic_price_rub must be decimals")

    images: list[tuple[bytes, str]] = []
    for f in product_images:
        blob = await f.read()
        images.append((blob, f.content_type or "application/octet-stream"))

    broll_pair: tuple[bytes, str] | None = None
    if broll_video is not None and broll_video.filename:
        broll_blob = await broll_video.read()
        if broll_blob:
            broll_pair = (
                broll_blob,
                broll_video.content_type or "application/octet-stream",
            )

    try:
        j = create_makeugc_job_async(
            db,
            current_user,
            product_images=images,
            product_name=product_name,
            premium_brand=premium_brand,
            premium_price_rub=premium_dec,
            mimic_price_rub=mimic_dec,
            persona_style=persona_style,
            broll_video=broll_pair,
        )
    except MakeUGCValidationError as e:
        raise HTTPException(400, str(e))

    return MakeUGCJobResponse.from_model(j)


@router.get("/jobs/{jid}", response_model=MakeUGCJobResponse)
def get_job(
    jid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return MakeUGCJobResponse.from_model(_get_owned(db, current_user, jid))
