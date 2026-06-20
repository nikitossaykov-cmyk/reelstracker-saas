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
    "image/heic": "heic",
    "image/heif": "heif",
    "image/avif": "avif",
}


MAX_PRODUCT_IMAGES = 4


def create_makeugc_job_async(
    db: Session,
    user: User,
    *,
    product_images: list[tuple[bytes, str]],
    product_name: str,
    premium_brand: str,
    premium_price_rub: Decimal,
    mimic_price_rub: Decimal,
    persona_style: str,
) -> MakeUGCJob:
    """`product_images` is a list of (bytes, content_type) — 1..4 angles
    of the same bottle. The worker collages them into a single Flux
    Kontext input image for sharper label/shape reconstruction."""
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

    if not product_images:
        raise MakeUGCValidationError("at least one product image required")
    if len(product_images) > MAX_PRODUCT_IMAGES:
        raise MakeUGCValidationError(
            f"too many product images (max {MAX_PRODUCT_IMAGES})"
        )

    # Validate + collect per-image specs before any R2 upload.
    image_specs: list[tuple[bytes, str, str]] = []  # (bytes, ext, content_type)
    for idx, (blob, ct) in enumerate(product_images):
        if not blob:
            raise MakeUGCValidationError(f"image #{idx + 1} is empty")
        if len(blob) > MAX_PRODUCT_IMAGE_BYTES:
            raise MakeUGCValidationError(
                f"image #{idx + 1} too large "
                f"(max {MAX_PRODUCT_IMAGE_BYTES // (1024 * 1024)} MB)"
            )
        ext = ALLOWED_PRODUCT_CONTENT_TYPES.get(ct)
        if not ext:
            raise MakeUGCValidationError(
                f"image #{idx + 1}: unsupported type {ct}"
            )
        image_specs.append((blob, ext, ct))

    r2 = get_r2()
    key_uuid = uuid.uuid4().hex[:12]
    keys: list[str] = []
    for idx, (blob, ext, ct) in enumerate(image_specs):
        key = f"users/{user.id}/makeugc/{key_uuid}/product-{idx + 1}.{ext}"
        r2.upload_bytes(key, blob, content_type=ct)
        keys.append(key)

    job = MakeUGCJob(
        user_id=user.id,
        product_image_key=keys[0],  # legacy single-key column points at the first angle
        product_image_keys=keys,
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
