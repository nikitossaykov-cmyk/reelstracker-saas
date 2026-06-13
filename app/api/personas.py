"""REST API for the persona library.

Endpoints:
  GET    /api/personas/                — list current user's personas
  POST   /api/personas/                — create + enqueue (202)
  GET    /api/personas/{pid}           — poll one
  POST   /api/personas/{pid}/canonical — pick one of the candidates
  DELETE /api/personas/{pid}           — soft-delete

All routes are user-scoped; cross-user access returns 404.
"""
from __future__ import annotations

from typing import Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.persona import Persona, PersonaStatus
from app.models.user import User
from app.services.persona_service import (
    PersonaValidationError,
    create_persona_async,
    set_canonical,
    soft_delete_persona,
)


router = APIRouter()

StyleHint = Literal["editorial", "lifestyle", "studio", "street"]


class PersonaCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    bio: str = Field(min_length=1, max_length=512)
    style_hint: Optional[StyleHint] = None


class PersonaResponse(BaseModel):
    id: int
    name: str
    bio: str
    style_hint: Optional[str]
    status: str
    canonical_face_url: Optional[str]
    gallery_json: list
    error_message: Optional[str]

    @classmethod
    def from_model(cls, p: Persona) -> "PersonaResponse":
        return cls(
            id=p.id,
            name=p.name,
            bio=p.bio,
            style_hint=p.style_hint,
            status=p.status,
            canonical_face_url=p.canonical_face_url,
            gallery_json=p.gallery_json or [],
            error_message=p.error_message,
        )


class PersonaListResponse(BaseModel):
    items: list[PersonaResponse]


class SetCanonicalRequest(BaseModel):
    gallery_index: int = Field(ge=0)


def _get_owned(db: Session, user: User, pid: int) -> Persona:
    p = (
        db.query(Persona)
        .filter(
            Persona.id == pid,
            Persona.user_id == user.id,
            Persona.deleted_at.is_(None),
        )
        .first()
    )
    if not p:
        raise HTTPException(404, "persona not found")
    return p


@router.get("/", response_model=PersonaListResponse)
def list_personas(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(Persona)
        .filter(
            Persona.user_id == current_user.id,
            Persona.deleted_at.is_(None),
        )
        .order_by(Persona.created_at.desc())
        .all()
    )
    return PersonaListResponse(
        items=[PersonaResponse.from_model(r) for r in rows]
    )


@router.post(
    "/",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PersonaResponse,
)
def create_persona(
    data: PersonaCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        p = create_persona_async(
            db,
            current_user,
            name=data.name,
            bio=data.bio,
            style_hint=data.style_hint,
        )
    except PersonaValidationError as e:
        msg = str(e).lower()
        code = 409 if "already in use" in msg else 400
        raise HTTPException(code, str(e))
    return PersonaResponse.from_model(p)


@router.get("/{pid}", response_model=PersonaResponse)
def get_persona(
    pid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return PersonaResponse.from_model(_get_owned(db, current_user, pid))


@router.post("/{pid}/canonical", response_model=PersonaResponse)
def pick_canonical(
    pid: int,
    data: SetCanonicalRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = _get_owned(db, current_user, pid)
    try:
        set_canonical(db, p, gallery_index=data.gallery_index)
    except PersonaValidationError as e:
        raise HTTPException(400, str(e))
    return PersonaResponse.from_model(p)


@router.delete("/{pid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_persona(
    pid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = _get_owned(db, current_user, pid)
    soft_delete_persona(db, p)
    return None
