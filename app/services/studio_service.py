"""Studio job create orchestration — validate, upload inputs to R2,
insert a PENDING row; studio_worker drains it."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.storage import get_r2
from app.models.studio_job import StudioJob, StudioStatus
from app.models.user import User
from app.services.makeugc_service import (
    ALLOWED_BROLL_CONTENT_TYPES,
    ALLOWED_PRODUCT_CONTENT_TYPES,
    MAX_BROLL_BYTES,
    MAX_PRODUCT_IMAGE_BYTES,
    MAX_PRODUCT_IMAGES,
)


VOICE_STYLES = {"normal", "asmr"}


class StudioValidationError(ValueError):
    pass


def create_studio_job_async(
    db: Session,
    user: User,
    *,
    product_images: list[tuple[bytes, str]],
    product_name: str,
    brand: str,
    price_rub: Decimal,
    dupe_price_rub: Decimal,
    script_text: str | None,
    voice_style: str,
    captions_enabled: bool,
    cutaways_enabled: bool = True,
    hook_video: tuple[bytes, str] | None = None,
) -> StudioJob:
    product_name = (product_name or "").strip()
    brand = (brand or "").strip()
    if not product_name:
        raise StudioValidationError("product_name required")
    if not brand:
        raise StudioValidationError("brand required")
    if voice_style not in VOICE_STYLES:
        raise StudioValidationError(
            f"unknown voice_style: {voice_style} (allowed: {sorted(VOICE_STYLES)})"
        )
    if price_rub <= 0 or dupe_price_rub <= 0:
        raise StudioValidationError("prices must be > 0")
    if not product_images:
        raise StudioValidationError("at least one product image required")
    if len(product_images) > MAX_PRODUCT_IMAGES:
        raise StudioValidationError(
            f"too many product images (max {MAX_PRODUCT_IMAGES})"
        )

    image_specs: list[tuple[bytes, str, str]] = []
    for idx, (blob, ct) in enumerate(product_images):
        if not blob:
            raise StudioValidationError(f"image #{idx + 1} is empty")
        if len(blob) > MAX_PRODUCT_IMAGE_BYTES:
            raise StudioValidationError(
                f"image #{idx + 1} too large "
                f"(max {MAX_PRODUCT_IMAGE_BYTES // (1024 * 1024)} MB)"
            )
        ext = ALLOWED_PRODUCT_CONTENT_TYPES.get(ct)
        if not ext:
            raise StudioValidationError(f"image #{idx + 1}: unsupported type {ct}")
        if ext in ("heic", "heif", "avif"):
            try:
                import io
                from app.services.strategy_makeugc.collage import _open_image
                img = _open_image(blob)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=92)
                blob, ext, ct = buf.getvalue(), "jpg", "image/jpeg"
            except Exception as e:
                raise StudioValidationError(
                    f"image #{idx + 1}: cannot decode HEIC/AVIF — {e}"
                )
        image_specs.append((blob, ext, ct))

    hook_spec: tuple[bytes, str, str] | None = None
    if hook_video:
        blob, ct = hook_video
        if not blob:
            raise StudioValidationError("hook video is empty")
        if len(blob) > MAX_BROLL_BYTES:
            raise StudioValidationError(
                f"hook video too large (max {MAX_BROLL_BYTES // (1024 * 1024)} MB)"
            )
        ext = ALLOWED_BROLL_CONTENT_TYPES.get(ct)
        if not ext:
            raise StudioValidationError(f"unsupported hook video type: {ct}")
        hook_spec = (blob, ext, ct)

    r2 = get_r2()
    key_uuid = uuid.uuid4().hex[:12]
    keys: list[str] = []
    for idx, (blob, ext, ct) in enumerate(image_specs):
        key = f"users/{user.id}/studio/{key_uuid}/product-{idx + 1}.{ext}"
        r2.upload_bytes(key, blob, content_type=ct)
        keys.append(key)

    hook_key: str | None = None
    if hook_spec:
        blob, ext, ct = hook_spec
        hook_key = f"users/{user.id}/studio/{key_uuid}/hook.{ext}"
        r2.upload_bytes(hook_key, blob, content_type=ct)

    job = StudioJob(
        user_id=user.id,
        product_image_keys=keys,
        product_name=product_name,
        brand=brand,
        price_rub=price_rub,
        dupe_price_rub=dupe_price_rub,
        script_text=(script_text or "").strip() or None,
        voice_style=voice_style,
        captions_enabled=captions_enabled,
        cutaways_enabled=cutaways_enabled,
        hook_video_key=hook_key,
        status=StudioStatus.PENDING,
        cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
