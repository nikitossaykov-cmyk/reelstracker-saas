"""StudioJob — async job row for the single-take UGC Studio (POC).

Lifecycle:
  PENDING → PORTRAIT → VOICEOVER → LIPSYNC → ASSEMBLE → JUDGE → READY
                                                               ↘ FAILED

Judge is non-blocking: a judge failure still lands the job in READY
with judge_score NULL (UI shows «QC недоступен»).
"""
from __future__ import annotations

import enum

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON,
    Numeric, String, Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


class StudioStatus(str, enum.Enum):
    PENDING = "pending"
    PORTRAIT = "portrait"
    VOICEOVER = "voiceover"
    LIPSYNC = "lipsync"
    CUTAWAYS = "cutaways"
    ASSEMBLE = "assemble"
    JUDGE = "judge"
    READY = "ready"
    FAILED = "failed"


class StudioJob(Base):
    __tablename__ = "studio_jobs"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )

    # Inputs
    product_image_keys = Column(JSON, nullable=False, default=list)
    product_name = Column(String(128), nullable=False)
    brand = Column(String(128), nullable=False)
    price_rub = Column(Numeric(10, 2), nullable=False)
    dupe_price_rub = Column(Numeric(10, 2), nullable=False)
    script_text = Column(Text, nullable=True)
    voice_style = Column(String(16), nullable=False, default="normal")  # normal|asmr
    captions_enabled = Column(Boolean, nullable=False, default=True)
    hook_video_key = Column(Text, nullable=True)
    cutaways_enabled = Column(Boolean, nullable=False, default=True)
    use_persona = Column(Boolean, nullable=False, default=True)
    look_prompt = Column(Text, nullable=True)

    # Stage outputs
    portrait_key = Column(Text, nullable=True)
    voiceover_key = Column(Text, nullable=True)
    lipsync_key = Column(Text, nullable=True)
    cap_still_key = Column(Text, nullable=True)
    spray_still_key = Column(Text, nullable=True)
    cap_clip_key = Column(Text, nullable=True)
    spray_clip_key = Column(Text, nullable=True)
    output_key = Column(Text, nullable=True)
    judge_score = Column(Integer, nullable=True)
    judge_report = Column(JSON, nullable=True)

    status = Column(String(24), nullable=False, default=StudioStatus.PENDING)
    error_message = Column(Text, nullable=True)
    cost_usd = Column(Numeric(8, 4), nullable=False, default=0)

    created_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_studio_jobs_user_id", "user_id"),
        Index("ix_studio_jobs_status", "status"),
    )

    user = relationship("User")
