"""Strategy E enqueue + validation. Worker drains.

Architecture note: this service is intentionally thin — it validates,
inserts a generated_videos row in PENDING, and returns. All actual work
(download, face-swap call, faststart, R2 upload) happens in the
forge_e_worker so the web request is fast and the work survives a
Railway redeploy.

See docs/specs/2026-06-13-forge-strategy-e-design.md §4 for the full
flow and §5.2 for the rationale.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.generation import GeneratedVideo, GenerationStatus, VideoProvider
from app.models.persona import Persona, PersonaStatus
from app.models.user import User


class ForgeEValidationError(ValueError):
    pass


def start_e(
    db: Session,
    user: User,
    *,
    source_url: str,
    persona_id: int,
    mode: int,
) -> GeneratedVideo:
    if mode not in (1, 2):
        raise ForgeEValidationError(f"unknown mode: {mode}")
    if not source_url or len(source_url) < 10:
        raise ForgeEValidationError("source_url required")

    persona = (
        db.query(Persona)
        .filter(
            Persona.id == persona_id,
            Persona.user_id == user.id,
            Persona.deleted_at.is_(None),
        )
        .first()
    )
    if not persona:
        raise ForgeEValidationError("persona not found")
    if persona.status != PersonaStatus.READY:
        raise ForgeEValidationError(
            f"persona not ready (status={persona.status})"
        )

    if mode == 1 and not user.replicate_api_key:
        raise ForgeEValidationError(
            "Mode 1 requires Replicate API key in profile"
        )

    gv = GeneratedVideo(
        user_id=user.id,
        prompt=(
            f"[strategy=E mode={mode} persona={persona.name}] "
            f"source={source_url}"
        ),
        provider=VideoProvider.MOCK,
        status=GenerationStatus.PENDING,
        persona_id=persona.id,
        mode=mode,
    )
    db.add(gv)
    db.commit()
    db.refresh(gv)
    return gv
