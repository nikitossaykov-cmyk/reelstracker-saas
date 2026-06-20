"""MakeUGC job create/list orchestration.

create_makeugc_job_async:
  - Validates required inputs.
  - Uploads the product image to R2 under a stable per-user key.
  - Inserts a row in PENDING state and returns immediately.
  - The makeugc_worker drains it and walks through the pipeline stages.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.storage import get_r2
from app.models.makeugc_job import MakeUGCJob, MakeUGCStatus
from app.models.user import User
from app.services.strategy_makeugc.portrait import VALID_STYLES


class MakeUGCValidationError(ValueError):
    pass


MAX_PRODUCT_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB

ALLOWED_PRODUCT_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def create_makeugc_job_async(
    db: Session,
    user: User,
    *,
    product_image_bytes: bytes,
    product_image_content_type: str,
    product_name: str,
    premium_brand: str,
    premium_price_rub: Decimal,
    mimic_price_rub: Decimal,
    persona_style: str,
) -> MakeUGCJob:
    product_name = (product_name or "").strip()
    premium_brand = (premium_brand or "").strip()
    if not product_name:
        raise MakeUGCValidationError("product_name required")
    if not premium_brand:
        raise MakeUGCValidationError("premium_brand required")
    if persona_style not in VALID_STYLES:
        raise MakeUGCValidationError(
            f"unknown persona_style: {persona_style} "
            f"(allowed: {sorted(VALID_STYLES)})"
        )
    if premium_price_rub <= 0:
        raise MakeUGCValidationError("premium_price_rub must be > 0")
    if mimic_price_rub <= 0:
        raise MakeUGCValidationError("mimic_price_rub must be > 0")

    if not product_image_bytes:
        raise MakeUGCValidationError("product image is empty")
    if len(product_image_bytes) > MAX_PRODUCT_IMAGE_BYTES:
        raise MakeUGCValidationError(
            f"product image too large "
            f"(max {MAX_PRODUCT_IMAGE_BYTES // (1024 * 1024)} MB)"
        )
    ext = ALLOWED_PRODUCT_CONTENT_TYPES.get(product_image_content_type)
    if not ext:
        raise MakeUGCValidationError(
            f"unsupported product image type: {product_image_content_type}"
        )

    # Upload product image to R2 under a key the user owns; key is
    # `users/<uid>/makeugc/<job_uuid_prefix>/product.<ext>` — we don't
    # yet know the job_id (auto-increment), so use a uuid prefix in the
    # path. Store the key on the row.
    r2 = get_r2()
    key_uuid = uuid.uuid4().hex[:12]
    product_key = f"users/{user.id}/makeugc/{key_uuid}/product.{ext}"
    r2.upload_bytes(
        product_key, product_image_bytes, content_type=product_image_content_type
    )

    job = MakeUGCJob(
        user_id=user.id,
        product_image_key=product_key,
        product_name=product_name,
        premium_brand=premium_brand,
        premium_price_rub=premium_price_rub,
        mimic_price_rub=mimic_price_rub,
        persona_style=persona_style,
        status=MakeUGCStatus.PENDING,
        cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
