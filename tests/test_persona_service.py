"""DB-backed tests for the persona service.

Opt-in: require TEST_DATABASE_URL via the conftest.db_engine fixture.
"""
import pytest

from app.models.persona import Persona, PersonaStatus
from app.services.persona_service import (
    PersonaValidationError,
    create_persona_async,
    set_canonical,
    soft_delete_persona,
)


def test_create_inserts_row_in_generating(db_session, test_user):
    p = create_persona_async(
        db_session, test_user,
        name="anya", bio="блондинка 23", style_hint="studio",
    )
    db_session.refresh(p)
    assert p.id is not None
    assert p.status == PersonaStatus.GENERATING
    assert p.gallery_json == []
    assert p.canonical_face_url is None
    assert p.user_id == test_user.id


def test_duplicate_name_rejected(db_session, test_user):
    create_persona_async(
        db_session, test_user, name="dup", bio="x", style_hint=None,
    )
    with pytest.raises(PersonaValidationError):
        create_persona_async(
            db_session, test_user, name="dup", bio="y", style_hint=None,
        )


def test_name_required(db_session, test_user):
    with pytest.raises(PersonaValidationError):
        create_persona_async(
            db_session, test_user, name="", bio="x", style_hint=None,
        )


def test_bio_required(db_session, test_user):
    with pytest.raises(PersonaValidationError):
        create_persona_async(
            db_session, test_user, name="ok", bio="", style_hint=None,
        )


def test_unknown_style_hint_rejected(db_session, test_user):
    with pytest.raises(PersonaValidationError):
        create_persona_async(
            db_session, test_user,
            name="ok", bio="x", style_hint="cinematic-vintage",
        )


def test_set_canonical_moves_to_ready(db_session, ready_persona):
    # ready_persona fixture is already READY — make a fresh AWAITING one
    from datetime import datetime
    p = Persona(
        user_id=ready_persona.user_id,
        name="awaiting-p",
        bio="x",
        status=PersonaStatus.AWAITING_CANONICAL,
        gallery_json=[
            {"url": "/m?0.png", "seed": 1, "index": 0},
            {"url": "/m?1.png", "seed": 2, "index": 1},
        ],
        created_at=datetime.utcnow(),
    )
    db_session.add(p); db_session.commit(); db_session.refresh(p)
    set_canonical(db_session, p, gallery_index=1)
    db_session.refresh(p)
    assert p.status == PersonaStatus.READY
    assert p.canonical_face_url == "/m?1.png"
    assert p.ready_at is not None


def test_set_canonical_out_of_range(db_session, ready_persona):
    from datetime import datetime
    p = Persona(
        user_id=ready_persona.user_id,
        name="awaiting-oor",
        bio="x",
        status=PersonaStatus.AWAITING_CANONICAL,
        gallery_json=[{"url": "/m?0.png", "seed": 1, "index": 0}],
        created_at=datetime.utcnow(),
    )
    db_session.add(p); db_session.commit(); db_session.refresh(p)
    with pytest.raises(PersonaValidationError):
        set_canonical(db_session, p, gallery_index=5)


def test_set_canonical_wrong_status(db_session, generating_persona):
    with pytest.raises(PersonaValidationError):
        set_canonical(db_session, generating_persona, gallery_index=0)


def test_soft_delete_sets_deleted_at(db_session, ready_persona):
    soft_delete_persona(db_session, ready_persona)
    db_session.refresh(ready_persona)
    assert ready_persona.deleted_at is not None
