"""DB-backed tests for start_e — validation + enqueue."""
import pytest

from app.models.generation import GenerationStatus, VideoProvider
from app.services.forge_e_service import ForgeEValidationError, start_e


def test_enqueues_row(db_session, test_user, ready_persona):
    gv = start_e(
        db_session, test_user,
        source_url="https://www.instagram.com/reel/X/",
        persona_id=ready_persona.id,
        mode=1,
    )
    assert gv.id is not None
    assert gv.status == GenerationStatus.PENDING
    assert gv.persona_id == ready_persona.id
    assert gv.mode == 1
    assert gv.provider == VideoProvider.MOCK


def test_rejects_unknown_mode(db_session, test_user, ready_persona):
    with pytest.raises(ForgeEValidationError):
        start_e(
            db_session, test_user,
            source_url="https://x.example/v",
            persona_id=ready_persona.id,
            mode=99,
        )


def test_rejects_too_short_url(db_session, test_user, ready_persona):
    with pytest.raises(ForgeEValidationError):
        start_e(
            db_session, test_user,
            source_url="x",
            persona_id=ready_persona.id,
            mode=1,
        )


def test_rejects_persona_not_owned(db_session, test_user, other_user_persona):
    with pytest.raises(ForgeEValidationError):
        start_e(
            db_session, test_user,
            source_url="https://x.example/v-long-enough",
            persona_id=other_user_persona.id,
            mode=1,
        )


def test_rejects_persona_not_ready(db_session, test_user, generating_persona):
    with pytest.raises(ForgeEValidationError):
        start_e(
            db_session, test_user,
            source_url="https://x.example/v-long-enough",
            persona_id=generating_persona.id,
            mode=1,
        )


def test_mode1_requires_replicate_key(db_session, ready_persona):
    from app.models.user import User
    from datetime import datetime
    nokey = User(
        email="nokey@example.com",
        password_hash="x",
        replicate_api_key=None,
    )
    db_session.add(nokey); db_session.commit(); db_session.refresh(nokey)
    # Attach a ready persona to this user
    from app.models.persona import Persona, PersonaStatus
    p = Persona(
        user_id=nokey.id, name="rp",
        bio="x", status=PersonaStatus.READY,
        canonical_face_url="/m?f.png",
        gallery_json=[], created_at=datetime.utcnow(),
    )
    db_session.add(p); db_session.commit(); db_session.refresh(p)

    with pytest.raises(ForgeEValidationError) as ei:
        start_e(
            db_session, nokey,
            source_url="https://x.example/v-long-enough",
            persona_id=p.id,
            mode=1,
        )
    assert "replicate" in str(ei.value).lower()
