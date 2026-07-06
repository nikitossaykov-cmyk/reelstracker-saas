"""Studio POC — service + worker orchestration tests."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

# Module-level import so the table registers on Base before conftest's
# create_all runs (same reason the other DB test files import models here).
from app.models.studio_job import StudioJob, StudioStatus


def test_studio_job_roundtrip(db_session, test_user):
    j = StudioJob(
        user_id=test_user.id,
        product_image_keys=["users/1/studio/x/product-1.jpg"],
        product_name="WHITE CHOCOLATE",
        brand="dose",
        price_rub=Decimal("1990"),
        dupe_price_rub=Decimal("16000"),
        voice_style="asmr",
        captions_enabled=True,
        status=StudioStatus.PENDING,
        cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    db_session.add(j)
    db_session.commit()
    db_session.refresh(j)

    assert j.id is not None
    assert j.status == StudioStatus.PENDING
    assert j.judge_score is None
    assert j.hook_video_key is None
