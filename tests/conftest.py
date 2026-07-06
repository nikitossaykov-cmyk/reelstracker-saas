"""Shared pytest fixtures.

Pure-Python tests (`test_persona_prompt.py`, `test_replicate_client.py`,
`test_runpod_pod.py`) don't touch the DB at all and skip these fixtures.

DB-backed fixtures activate only when `TEST_DATABASE_URL` is exported.
This lets the project run the pure suite anywhere (CI, dev) while
DB-dependent tests opt in. To enable locally:

    createdb reelstracker_test
    export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/reelstracker_test
    pytest tests/ -v
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest


# Use TEST_DATABASE_URL when set (real Postgres for prod-parity tests);
# otherwise fall back to in-memory SQLite for fast / dep-free local runs.
# All models in this codebase use cross-dialect SQLAlchemy types — JSONB
# was replaced with JSON specifically to make this fallback viable.
TEST_DB_URL = os.getenv("TEST_DATABASE_URL") or "sqlite:///:memory:"


# ─── DB-backed fixtures ────────────────────────────────────────────────

@pytest.fixture(scope="session")
def db_engine():
    from sqlalchemy import create_engine
    from app.database import Base
    if TEST_DB_URL.startswith("sqlite"):
        # in-memory shared between connections within the session
        engine = create_engine(
            TEST_DB_URL,
            connect_args={"check_same_thread": False},
        )
    else:
        engine = create_engine(TEST_DB_URL)
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    from sqlalchemy.orm import sessionmaker
    conn = db_engine.connect()
    trans = conn.begin()
    Session = sessionmaker(bind=conn, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()


@pytest.fixture
def test_user(db_session):
    from app.models.user import User
    u = User(
        email="t@example.com",
        hashed_password="x",
        replicate_api_key="rk-test",
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def other_user(db_session):
    from app.models.user import User
    u = User(email="o@example.com", hashed_password="x")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def ready_persona(db_session, test_user):
    from app.models.persona import Persona, PersonaStatus
    p = Persona(
        user_id=test_user.id,
        name="ready-p",
        bio="блондинка 23, мягкий студийный свет",
        status=PersonaStatus.READY,
        canonical_face_url="/api/media?key=u/1/personas/1/seed.png",
        gallery_json=[
            {"url": "/api/media?key=u/1/personas/1/seed.png",
             "seed": 101, "index": 0},
        ],
        created_at=datetime.utcnow(),
        ready_at=datetime.utcnow(),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def generating_persona(db_session, test_user):
    from app.models.persona import Persona, PersonaStatus
    p = Persona(
        user_id=test_user.id,
        name="gen-p",
        bio="x",
        status=PersonaStatus.GENERATING,
        gallery_json=[],
        created_at=datetime.utcnow(),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def other_user_persona(db_session, other_user):
    from app.models.persona import Persona, PersonaStatus
    p = Persona(
        user_id=other_user.id,
        name="other-p",
        bio="x",
        status=PersonaStatus.READY,
        canonical_face_url="/api/media?key=u/2/p/x.png",
        gallery_json=[],
        created_at=datetime.utcnow(),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def auth_client(test_user, db_session):
    from fastapi.testclient import TestClient
    from app.api.deps import get_current_user
    from app.database import get_db
    from app.main import app
    app.dependency_overrides[get_current_user] = lambda: test_user
    app.dependency_overrides[get_db] = lambda: db_session
    # No context manager: lifespan would create_all on the real
    # DATABASE_URL and spawn worker threads — tests need neither.
    yield TestClient(app)
    app.dependency_overrides.clear()
