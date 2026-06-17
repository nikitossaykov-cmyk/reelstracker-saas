"""Drains MakeUGCJob rows through the pipeline stages.

Scaffold PR scope: PENDING → PORTRAIT → READY (skips voiceover / lipsync
/ cutaway / concat — those land in subsequent PRs as their modules
arrive). For now READY just means "the portrait stage succeeded" — UI
shows portrait image as proof the architecture works end-to-end on
Railway.

Row locking via SELECT ... FOR UPDATE SKIP LOCKED so the loop can be
moved to a separate process later without losing exactly-once
semantics.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.storage import get_r2
from app.models.makeugc_job import MakeUGCJob, MakeUGCStatus
from app.services.media_helpers import download_bytes
from app.services.replicate_client import (
    ReplicateError,
    ReplicateSafetyError,
    ReplicateTransientError,
)
from app.services.strategy_makeugc.portrait import (
    MODEL_MAX,
    generate_portrait,
)


log = logging.getLogger(__name__)


def pick_next_pending(db: Session) -> MakeUGCJob | None:
    return (
        db.query(MakeUGCJob)
        .filter(MakeUGCJob.status == MakeUGCStatus.PENDING)
        .order_by(MakeUGCJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )


def _fail(db: Session, j: MakeUGCJob, msg: str) -> None:
    j.status = MakeUGCStatus.FAILED
    j.error_message = msg[:500]
    j.completed_at = datetime.utcnow()
    db.commit()


def _mark_stage(db: Session, j: MakeUGCJob, stage: MakeUGCStatus) -> None:
    j.status = stage
    db.commit()


def process_job(db: Session, j: MakeUGCJob, replicate_api_key: str) -> None:
    if not replicate_api_key:
        _fail(db, j, "У пользователя нет Replicate API key — задай его в /settings")
        return

    # --- Stage: PORTRAIT ---
    _mark_stage(db, j, MakeUGCStatus.PORTRAIT)
    r2 = get_r2()

    try:
        # Fetch product image from R2 → bytes
        obj = r2._client.get_object(Bucket=r2.bucket, Key=j.product_image_key)
        product_bytes = obj["Body"].read()
        product_ct = obj.get("ContentType") or "image/jpeg"

        url, cost = generate_portrait(
            product_image_bytes=product_bytes,
            product_content_type=product_ct,
            persona_style=j.persona_style,
            replicate_api_key=replicate_api_key,
            model=MODEL_MAX,
        )

        blob = download_bytes(url, timeout=120)
        key = (
            f"users/{j.user_id}/makeugc/{j.id}/"
            f"portrait-{uuid.uuid4().hex[:6]}.jpg"
        )
        r2.upload_bytes(key, blob, content_type="image/jpeg")
        j.portrait_key = key
        j.cost_usd = (j.cost_usd or Decimal("0")) + Decimal(str(cost))
        db.commit()
    except ReplicateSafetyError as e:
        _fail(db, j, f"🛑 Moderation отбила portrait prompt: {str(e)[:200]}")
        return
    except ReplicateTransientError as e:
        log.warning("makeugc %s transient on portrait: %s", j.id, e)
        # leave row in PORTRAIT — outer loop will requeue via SKIP_LOCKED
        # next tick because we don't reset to PENDING (intentional: avoid
        # spinning forever; treat persistent transient as a fail after
        # one retry cycle).
        raise
    except ReplicateError as e:
        _fail(db, j, f"Replicate hard fail on portrait: {str(e)[:200]}")
        return
    except Exception as e:
        log.exception("makeugc %s unexpected fail on portrait", j.id)
        _fail(db, j, f"внутренняя ошибка portrait: {str(e)[:200]}")
        return

    # Scaffold PR: voiceover/lipsync/cutaway/concat not yet implemented.
    # Mark READY so the UI sees the portrait. Next-stage PRs will move
    # READY further down the pipeline.
    j.status = MakeUGCStatus.READY
    j.completed_at = datetime.utcnow()
    db.commit()


def run_loop(db_factory, poll_seconds: float = 2.0) -> None:
    """Long-running drain loop. db_factory() returns a fresh Session per tick."""
    while True:
        db = db_factory()
        try:
            j = pick_next_pending(db)
            if j is None:
                db.commit()
                time.sleep(poll_seconds)
                continue
            try:
                from app.models.user import User
                user = db.query(User).get(j.user_id)
                api_key = user.replicate_api_key if user else None
                process_job(db, j, replicate_api_key=api_key or "")
            except ReplicateTransientError:
                # Leave row's current status — next tick won't claim it
                # because SELECT FOR UPDATE SKIP LOCKED filters by
                # status=PENDING. Persistent transient = manual retry.
                db.rollback()
            except Exception as e:
                log.exception("makeugc %s hard fail", j.id)
                _fail(db, j, f"внутренняя ошибка: {str(e)[:200]}")
        finally:
            db.close()
