"""Persona — a per-user locked AI-fictional identity used by Forge Strategy E.

Lifecycle:
  GENERATING → AWAITING_CANONICAL → READY
                                  ↘
                                   FAILED (terminal)

A user creates one with name + bio + optional style. The persona_worker
generates 4 candidate face images via Replicate PuLID-Flux; the user
picks one as canonical via POST /api/personas/{id}/canonical. The
canonical_face_url is then used by Forge Strategy E as the face
reference for every video remixed with this persona.

deleted_at is set on soft-delete so historical generated_videos rows
that reference the persona don't dangle.
"""
from __future__ import annotations

import enum

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, JSON,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import relationship

from app.database import Base


class PersonaStatus(str, enum.Enum):
    GENERATING = "generating"
    AWAITING_CANONICAL = "awaiting_canonical"
    READY = "ready"
    FAILED = "failed"


class Persona(Base):
    __tablename__ = "personas"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(64), nullable=False)
    bio = Column(String(512), nullable=False)
    style_hint = Column(String(32), nullable=True)
    status = Column(String(24), nullable=False, default=PersonaStatus.GENERATING)
    canonical_face_url = Column(Text, nullable=True)
    gallery_json = Column(JSON, nullable=False, default=list)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False)
    ready_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_personas_user_name"),
        Index("ix_personas_user_id", "user_id"),
        Index("ix_personas_status", "status"),
    )

    user = relationship("User")
