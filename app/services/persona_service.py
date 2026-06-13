"""Persona create / canonical-pick / soft-delete orchestration.

create_persona_async only inserts the row in GENERATING state and
returns immediately. The persona_worker drains the row in a separate
loop, runs Replicate, populates gallery_json, and moves status to
AWAITING_CANONICAL. The user then picks one via set_canonical.

set_canonical only accepts a persona in AWAITING_CANONICAL — that
guards against accidental double-pick after the user has already
committed to a face for downstream Strategy E rows.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.persona import Persona, PersonaStatus
from app.models.user import User


VALID_STYLE_HINTS = {"editorial", "lifestyle", "studio", "street"}


class PersonaValidationError(ValueError):
    pass


def create_persona_async(
    db: Session,
    user: User,
    *,
    name: str,
    bio: str,
    style_hint: str | None,
) -> Persona:
    name = (name or "").strip()
    bio = (bio or "").strip()
    if not name:
        raise PersonaValidationError("name required")
    if not bio:
        raise PersonaValidationError("bio required")
    if len(name) > 64:
        raise PersonaValidationError("name too long (max 64)")
    if len(bio) > 512:
        raise PersonaValidationError("bio too long (max 512)")
    if style_hint and style_hint not in VALID_STYLE_HINTS:
        raise PersonaValidationError(f"unknown style_hint: {style_hint}")

    p = Persona(
        user_id=user.id,
        name=name,
        bio=bio,
        style_hint=style_hint,
        status=PersonaStatus.GENERATING,
        gallery_json=[],
        created_at=datetime.utcnow(),
    )
    db.add(p)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise PersonaValidationError(
            f"persona name already in use: {name}"
        ) from e
    db.refresh(p)
    return p


def set_canonical(db: Session, p: Persona, *, gallery_index: int) -> Persona:
    if p.status != PersonaStatus.AWAITING_CANONICAL:
        raise PersonaValidationError(
            f"persona must be in AWAITING_CANONICAL, got {p.status}"
        )
    gallery = p.gallery_json or []
    if not (0 <= gallery_index < len(gallery)):
        raise PersonaValidationError(
            f"gallery_index {gallery_index} out of range (have {len(gallery)})"
        )
    chosen = gallery[gallery_index]
    p.canonical_face_url = chosen["url"]
    p.status = PersonaStatus.READY
    p.ready_at = datetime.utcnow()
    db.commit()
    db.refresh(p)
    return p


def soft_delete_persona(db: Session, p: Persona) -> None:
    p.deleted_at = datetime.utcnow()
    db.commit()
