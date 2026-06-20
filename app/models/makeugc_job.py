"""MakeUGCJob — async job row for Strategy MakeUGC.

A MakeUGC job goes through several stages, each produced by a dedicated
pipeline module. This row tracks both the inputs (collected from the
user via POST /api/makeugc/jobs) and the URLs of intermediate + final
artifacts (each step writes its URL back when done).

Lifecycle:
  PENDING → PORTRAIT → VOICEOVER → LIPSYNC → CUTAWAY → CONCAT → READY
                                                              ↘
                                                               FAILED

Only PENDING → PORTRAIT → READY is wired in the scaffold PR — the rest
of the stages land as separate PRs (one per pipeline module).
"""
from __future__ import annotations

import enum

from sqlalchemy import (
    Column, Integer, String, Text, Numeric, DateTime, ForeignKey, Index,
)
from sqlalchemy.orm import relationship

from app.database import Base


class MakeUGCStatus(str, enum.Enum):
    PENDING = "pending"
    PORTRAIT = "portrait"
    VOICEOVER = "voiceover"
    LIPSYNC = "lipsync"
    CUTAWAY = "cutaway"
    CONCAT = "concat"
    READY = "ready"
    FAILED = "failed"


class MakeUGCJob(Base):
    __tablename__ = "makeugc_jobs"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Inputs (collected from POST /api/makeugc/jobs)
    product_image_key = Column(Text, nullable=False)
    product_name = Column(String(128), nullable=False)
    premium_brand = Column(String(128), nullable=False)
    premium_price_usd = Column(Numeric(8, 2), nullable=False)
    mimic_price_usd = Column(Numeric(8, 2), nullable=False)
    persona_style = Column(String(32), nullable=False, default="average-girl")

    # Stage outputs (filled as the worker progresses)
    portrait_key = Column(Text, nullable=True)
    voiceover_key = Column(Text, nullable=True)
    lipsync_key = Column(Text, nullable=True)
    bottle_hero_key = Column(Text, nullable=True)
    output_key = Column(Text, nullable=True)

    # Optional user-uploaded B-roll of their real bottle (5-10 sec). If
    # null, the worker auto-generates a Flux-Kontext-PRO bottle-hero
    # still and uses that as the cutaway. The real-video path closes the
    # tiny-label problem (handoff §75-78).
    broll_video_key = Column(Text, nullable=True)

    # The voiceover script — generated from premium_brand / prices /
    # product_name before TTS. Persisted so the user can see and (later)
    # tweak what the persona says.
    script_text = Column(Text, nullable=True)

    status = Column(String(24), nullable=False, default=MakeUGCStatus.PENDING)
    error_message = Column(Text, nullable=True)
    cost_usd = Column(Numeric(8, 4), nullable=False, default=0)

    created_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_makeugc_jobs_user_id", "user_id"),
        Index("ix_makeugc_jobs_status", "status"),
    )

    user = relationship("User")
