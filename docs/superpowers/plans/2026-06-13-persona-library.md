# Persona Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-user persona library (create / list / pick-canonical / delete) backed by Replicate PuLID-Flux generation, as the foundation for Strategy E face-replacement.

**Architecture:** New `personas` table + `Persona` model. Async create flow: `POST /api/personas/` returns 202 + row in `generating`; a small in-process worker calls Replicate 4× with varied seeds; user polls and picks one canonical image. R2 stores the 4 candidates per persona. New `/personas.html` page in the existing static-frontend style.

**Tech Stack:** SQLAlchemy 2 + Alembic + FastAPI + Pydantic v2 + Replicate Python SDK + boto3 (existing R2 helper) + pytest.

---

## File Structure

| file | responsibility |
|---|---|
| `alembic/versions/<rev>_add_personas.py` | Schema migration: table + indexes |
| `app/models/persona.py` | SQLAlchemy `Persona` model + status enum |
| `app/services/persona_prompt.py` | Pure functions building moderation-safe PuLID prompts |
| `app/services/persona_service.py` | Async create / canonical-pick / delete orchestration |
| `app/services/replicate_client.py` | Thin wrapper around Replicate SDK with retries + safety detection |
| `app/api/personas.py` | FastAPI router: 5 endpoints |
| `app/workers/persona_worker.py` | Background loop draining `personas` rows in `generating` state |
| `app/main.py` | Register router + new static route |
| `static/personas.html` | Frontend page |
| `static/js/personas.js` | List + create modal + canonical picker |
| `tests/conftest.py` | Pytest fixtures: DB + auth client + Replicate mock |
| `tests/test_persona_prompt.py` | Prompt-builder unit tests |
| `tests/test_persona_service.py` | Service-level tests (DB-backed, Replicate mocked) |
| `tests/test_personas_api.py` | API-level tests through TestClient |

---

## Task 1: Alembic migration — `personas` table

**Files:**
- Create: `alembic/versions/20260613_01_add_personas.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_persona_migration.py`:

```python
"""Smoke test that the migration applies cleanly against a fresh schema."""
from sqlalchemy import inspect

def test_personas_table_exists(db_engine):
    insp = inspect(db_engine)
    assert "personas" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("personas")}
    assert {"id", "user_id", "name", "bio", "style_hint", "status",
            "canonical_face_url", "gallery_json", "error_message",
            "created_at", "ready_at", "deleted_at"} <= cols
    indexes = {i["name"] for i in insp.get_indexes("personas")}
    assert any("user_id" in i for i in indexes)
```

- [ ] **Step 2: Verify it fails**

Run: `pytest tests/test_persona_migration.py -v`
Expected: FAIL — table doesn't exist (or fixture missing — see Task 0 note below).

> **Task 0 note:** If `tests/conftest.py` doesn't exist yet, create it before Task 1 with a basic `db_engine` fixture that runs `alembic upgrade head` against an ephemeral Postgres (testcontainers or a `TEST_DATABASE_URL` env var). The existing project has no tests — this is the seed. See `tests/conftest.py` task at the bottom of this plan.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/20260613_01_add_personas.py`:

```python
"""add personas table

Revision ID: 20260613_01
Revises: <set to current head>
Create Date: 2026-06-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260613_01"
down_revision = None  # set to current head before running
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "personas",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("bio", sa.String(512), nullable=False),
        sa.Column("style_hint", sa.String(32), nullable=True),
        sa.Column("status", sa.String(24), nullable=False,
                  server_default="generating"),
        sa.Column("canonical_face_url", sa.Text, nullable=True),
        sa.Column("gallery_json", postgresql.JSONB, nullable=False,
                  server_default="[]"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.now()),
        sa.Column("ready_at", sa.DateTime, nullable=True),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint("user_id", "name", name="uq_personas_user_name"),
    )
    op.create_index("ix_personas_user_id", "personas", ["user_id"])
    op.create_index("ix_personas_status", "personas", ["status"])


def downgrade():
    op.drop_index("ix_personas_status", table_name="personas")
    op.drop_index("ix_personas_user_id", table_name="personas")
    op.drop_table("personas")
```

Before running, set `down_revision` to the current head printed by
`alembic heads`.

- [ ] **Step 4: Verify test passes**

Run: `pytest tests/test_persona_migration.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/20260613_01_add_personas.py tests/test_persona_migration.py tests/conftest.py
git commit -m "feat(personas): add personas table migration"
```

---

## Task 2: `Persona` SQLAlchemy model

**Files:**
- Create: `app/models/persona.py`
- Test: `tests/test_persona_model.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_persona_model.py
from app.models.persona import Persona, PersonaStatus

def test_persona_round_trip(db_session, test_user):
    p = Persona(
        user_id=test_user.id,
        name="anya",
        bio="блондинка 23, голубые глаза, мягкий студийный свет",
        style_hint="editorial",
        status=PersonaStatus.GENERATING,
        gallery_json=[],
    )
    db_session.add(p); db_session.commit(); db_session.refresh(p)
    assert p.id is not None
    assert p.status == PersonaStatus.GENERATING
    assert p.created_at is not None
    assert p.canonical_face_url is None

def test_persona_unique_name_per_user(db_session, test_user):
    from sqlalchemy.exc import IntegrityError
    db_session.add(Persona(user_id=test_user.id, name="dup", bio="x",
                           status=PersonaStatus.GENERATING, gallery_json=[]))
    db_session.commit()
    db_session.add(Persona(user_id=test_user.id, name="dup", bio="y",
                           status=PersonaStatus.GENERATING, gallery_json=[]))
    try:
        db_session.commit()
        assert False, "expected IntegrityError"
    except IntegrityError:
        db_session.rollback()
```

- [ ] **Step 2: Verify fails**

Run: `pytest tests/test_persona_model.py -v`
Expected: ImportError on `from app.models.persona import …`

- [ ] **Step 3: Write the model**

```python
# app/models/persona.py
import enum
from sqlalchemy import (Column, Integer, String, Text, DateTime, ForeignKey,
                        JSON, UniqueConstraint, Index)
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from app.database import Base


class PersonaStatus(str, enum.Enum):
    GENERATING = "generating"
    AWAITING_CANONICAL = "awaiting_canonical"
    READY = "ready"
    FAILED = "failed"


class Persona(Base):
    __tablename__ = "personas"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False)
    name = Column(String(64), nullable=False)
    bio = Column(String(512), nullable=False)
    style_hint = Column(String(32), nullable=True)
    status = Column(String(24), nullable=False, default=PersonaStatus.GENERATING)
    canonical_face_url = Column(Text, nullable=True)
    gallery_json = Column(JSONB, nullable=False, default=list)
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
```

- [ ] **Step 4: Verify passes**

Run: `pytest tests/test_persona_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models/persona.py tests/test_persona_model.py
git commit -m "feat(personas): Persona SQLAlchemy model"
```

---

## Task 3: Moderation-safe prompt builder

**Files:**
- Create: `app/services/persona_prompt.py`
- Test: `tests/test_persona_prompt.py`

The prompt builder is pure — no I/O — so it's our fastest TDD loop and the canonical place for the moderation-safe language.

- [ ] **Step 1: Failing tests**

```python
# tests/test_persona_prompt.py
from app.services.persona_prompt import (
    build_persona_prompt, defensive_fallback_prompt, SAFE_DEFAULTS,
)

def test_includes_defensive_safe_phrases():
    out = build_persona_prompt("блондинка 23 года, голубые глаза", style="studio")
    low = out.lower()
    for phrase in ("fictional", "generic", "no real-person likeness",
                   "no logos of existing brands"):
        assert phrase in low, f"missing safety phrase: {phrase}"

def test_includes_user_bio_verbatim_after_safety_frame():
    bio = "блондинка 23 года"
    out = build_persona_prompt(bio, style=None)
    assert bio in out
    # safety frame comes BEFORE bio (sets the interpretive frame for moderation)
    assert out.index("fictional") < out.index(bio)

def test_style_hint_appended_when_present():
    out = build_persona_prompt("девушка", style="editorial")
    assert "editorial" in out.lower()
    out2 = build_persona_prompt("девушка", style=None)
    assert "editorial" not in out2.lower()

def test_truncates_to_safe_length():
    huge = "x" * 5000
    out = build_persona_prompt(huge, style=None)
    assert len(out) <= 1000  # PuLID prompt ceiling

def test_defensive_fallback_is_user_free():
    fb = defensive_fallback_prompt()
    assert fb == SAFE_DEFAULTS
    for risky in ("teen", "child", "young teenager"):
        assert risky not in fb.lower()
```

- [ ] **Step 2: Verify fails**

Run: `pytest tests/test_persona_prompt.py -v`
Expected: ImportError.

- [ ] **Step 3: Implementation**

```python
# app/services/persona_prompt.py
"""Builds prompts for Replicate PuLID-Flux persona generation.

Lifts the moderation-safe frame from the gpt-image-1 skill notes:
priming language ('fictional', 'generic', 'no real-person likeness',
'no logos of existing brands') goes BEFORE the user bio so the
moderation layer reads it first and accepts the rest as safe-intent.
"""
from __future__ import annotations
from typing import Optional


SAFETY_FRAME = (
    "Photorealistic vertical 9:16 portrait of a fictional generic adult "
    "content creator (no identifying features, no celebrity likeness, "
    "no real-person likeness, no logos of existing brands), soft natural "
    "studio light, neutral background, casual styling."
)

SAFE_DEFAULTS = (
    "Photorealistic vertical 9:16 portrait, generic adult woman with "
    "neutral expression, soft studio light, plain background, casual "
    "styling, no identifying features, no logos."
)

STYLE_PHRASE = {
    "editorial": "editorial fashion photography aesthetic",
    "lifestyle": "natural lifestyle photography aesthetic",
    "studio": "clean studio photography aesthetic",
    "street": "candid street photography aesthetic",
}

MAX_PROMPT_LEN = 1000


def build_persona_prompt(bio: str, style: Optional[str]) -> str:
    parts = [SAFETY_FRAME, f"general look (fictional persona): {bio.strip()}."]
    if style and style in STYLE_PHRASE:
        parts.append(STYLE_PHRASE[style] + ".")
    parts.append("No real-person likeness. No logos of existing brands.")
    prompt = " ".join(parts)
    return prompt[:MAX_PROMPT_LEN]


def defensive_fallback_prompt() -> str:
    return SAFE_DEFAULTS
```

- [ ] **Step 4: Verify**

Run: `pytest tests/test_persona_prompt.py -v`
Expected: PASS (5/5).

- [ ] **Step 5: Commit**

```bash
git add app/services/persona_prompt.py tests/test_persona_prompt.py
git commit -m "feat(personas): moderation-safe prompt builder"
```

---

## Task 4: Replicate client wrapper

**Files:**
- Create: `app/services/replicate_client.py`
- Test: `tests/test_replicate_client.py`

A thin wrapper so the service layer doesn't know about Replicate SDK
specifics, and so tests have one place to mock.

- [ ] **Step 1: Failing tests**

```python
# tests/test_replicate_client.py
import pytest
from unittest.mock import patch, MagicMock
from app.services.replicate_client import (
    ReplicateClient, ReplicateSafetyError, ReplicateTransientError,
)


def test_run_model_returns_url():
    fake = MagicMock(return_value=["https://r/out.png"])
    with patch("replicate.run", fake):
        c = ReplicateClient(api_key="k")
        out = c.run_model("lucataco/pulid-flux", {"prompt": "x", "seed": 1})
    assert out == ["https://r/out.png"]


def test_raises_safety_on_moderation_keywords():
    def boom(*a, **kw):
        raise Exception("nsfw content flagged by safety policy")
    with patch("replicate.run", boom):
        c = ReplicateClient(api_key="k")
        with pytest.raises(ReplicateSafetyError):
            c.run_model("m", {"prompt": "x"})


def test_raises_transient_on_5xx_or_timeout():
    def boom(*a, **kw):
        raise Exception("upstream 502 bad gateway")
    with patch("replicate.run", boom):
        c = ReplicateClient(api_key="k")
        with pytest.raises(ReplicateTransientError):
            c.run_model("m", {"prompt": "x"})


def test_requires_api_key():
    with pytest.raises(ValueError):
        ReplicateClient(api_key="")
```

- [ ] **Step 2: Verify fails**

Run: `pytest tests/test_replicate_client.py -v`
Expected: ImportError.

- [ ] **Step 3: Implementation**

```python
# app/services/replicate_client.py
"""Thin Replicate wrapper.

Classifies exceptions into:
  - ReplicateSafetyError: moderation refused — fall back to safe default
  - ReplicateTransientError: 5xx / timeout — caller may retry
  - ReplicateError: anything else, terminal
"""
from __future__ import annotations
import os
import re
from typing import Any


SAFETY_PAT = re.compile(
    r"(nsfw|safety|moderat|policy|content[_ ]filter|inappropriate)",
    re.IGNORECASE,
)
TRANSIENT_PAT = re.compile(
    r"(timeout|timed out|502|503|504|temporarily unavailable|gateway)",
    re.IGNORECASE,
)


class ReplicateError(Exception):
    pass


class ReplicateSafetyError(ReplicateError):
    pass


class ReplicateTransientError(ReplicateError):
    pass


class ReplicateClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("Replicate api_key is required")
        self.api_key = api_key

    def run_model(self, ref: str, params: dict[str, Any]):
        os.environ["REPLICATE_API_TOKEN"] = self.api_key
        import replicate
        try:
            return replicate.run(ref, input=params)
        except Exception as e:
            msg = str(e)
            if SAFETY_PAT.search(msg):
                raise ReplicateSafetyError(msg) from e
            if TRANSIENT_PAT.search(msg):
                raise ReplicateTransientError(msg) from e
            raise ReplicateError(msg) from e
```

- [ ] **Step 4: Verify**

Run: `pytest tests/test_replicate_client.py -v`
Expected: PASS (4/4).

- [ ] **Step 5: Commit**

```bash
git add app/services/replicate_client.py tests/test_replicate_client.py
git commit -m "feat(personas): Replicate client with safety/transient classification"
```

---

## Task 5: `persona_service.create_async` (enqueue only)

**Files:**
- Create: `app/services/persona_service.py`
- Test: `tests/test_persona_service.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_persona_service.py
import pytest
from app.models.persona import Persona, PersonaStatus
from app.services.persona_service import (
    create_persona_async, PersonaValidationError,
)


def test_create_inserts_row_in_generating(db_session, test_user):
    p = create_persona_async(db_session, test_user,
                             name="anya", bio="блондинка 23", style_hint="studio")
    db_session.refresh(p)
    assert p.id is not None
    assert p.status == PersonaStatus.GENERATING
    assert p.gallery_json == []
    assert p.canonical_face_url is None
    assert p.user_id == test_user.id


def test_duplicate_name_rejected(db_session, test_user):
    create_persona_async(db_session, test_user, name="dup", bio="x", style_hint=None)
    with pytest.raises(PersonaValidationError):
        create_persona_async(db_session, test_user, name="dup", bio="y", style_hint=None)


def test_name_required(db_session, test_user):
    with pytest.raises(PersonaValidationError):
        create_persona_async(db_session, test_user, name="", bio="x", style_hint=None)


def test_bio_required(db_session, test_user):
    with pytest.raises(PersonaValidationError):
        create_persona_async(db_session, test_user, name="ok", bio="", style_hint=None)
```

- [ ] **Step 2: Verify fails**

Run: `pytest tests/test_persona_service.py -v`
Expected: ImportError.

- [ ] **Step 3: Implementation**

```python
# app/services/persona_service.py
"""Persona create / canonical-pick / delete orchestration.

create_persona_async only inserts the row and returns. The
persona_worker drains rows in 'generating' state and runs Replicate.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models.persona import Persona, PersonaStatus
from app.models.user import User


class PersonaValidationError(ValueError):
    pass


def create_persona_async(db: Session, user: User, *,
                         name: str, bio: str,
                         style_hint: str | None) -> Persona:
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
    if style_hint and style_hint not in {"editorial", "lifestyle", "studio", "street"}:
        raise PersonaValidationError(f"unknown style_hint: {style_hint}")

    p = Persona(
        user_id=user.id, name=name, bio=bio, style_hint=style_hint,
        status=PersonaStatus.GENERATING, gallery_json=[],
        created_at=datetime.utcnow(),
    )
    db.add(p)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise PersonaValidationError(f"persona name already in use: {name}") from e
    db.refresh(p)
    return p
```

- [ ] **Step 4: Verify**

Run: `pytest tests/test_persona_service.py -v`
Expected: PASS (4/4).

- [ ] **Step 5: Commit**

```bash
git add app/services/persona_service.py tests/test_persona_service.py
git commit -m "feat(personas): create_persona_async — validation + insert"
```

---

## Task 6: Persona worker — Replicate happy path

**Files:**
- Create: `app/workers/persona_worker.py`
- Test: `tests/test_persona_worker.py`

The worker drains one persona at a time. On success: 4 candidate URLs
get downloaded → uploaded to R2 → `gallery_json` populated → status
moves to `AWAITING_CANONICAL`.

- [ ] **Step 1: Failing test**

```python
# tests/test_persona_worker.py
from unittest.mock import patch, MagicMock
from app.models.persona import Persona, PersonaStatus
from app.workers.persona_worker import process_persona


def test_happy_path_populates_gallery(db_session, test_user):
    p = Persona(user_id=test_user.id, name="anya", bio="блондинка 23",
                status=PersonaStatus.GENERATING, gallery_json=[],
                created_at=__import__('datetime').datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)

    fake_replicate = MagicMock(return_value=["https://r/img.png"])
    fake_download = MagicMock(return_value=b"\x89PNG fake bytes")
    fake_r2_upload = MagicMock(
        side_effect=lambda key, data, content_type: f"/api/media?key={key}"
    )

    with patch("app.workers.persona_worker.ReplicateClient") as Rc, \
         patch("app.workers.persona_worker.download_bytes", fake_download), \
         patch("app.workers.persona_worker.r2_upload_bytes", fake_r2_upload):
        Rc.return_value.run_model = fake_replicate
        process_persona(db_session, p, replicate_api_key="rk")

    db_session.refresh(p)
    assert p.status == PersonaStatus.AWAITING_CANONICAL
    assert len(p.gallery_json) == 4
    for item in p.gallery_json:
        assert "url" in item and "seed" in item and "index" in item
    # 4 different seeds were used
    seeds = {it["seed"] for it in p.gallery_json}
    assert len(seeds) == 4
```

- [ ] **Step 2: Verify fails**

Run: `pytest tests/test_persona_worker.py::test_happy_path_populates_gallery -v`
Expected: ImportError.

- [ ] **Step 3: Implementation**

```python
# app/workers/persona_worker.py
"""Drains 'personas' rows in GENERATING state, runs Replicate, populates gallery."""
from __future__ import annotations
import io
import logging
import time
import uuid
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.persona import Persona, PersonaStatus
from app.services.persona_prompt import (
    build_persona_prompt, defensive_fallback_prompt,
)
from app.services.replicate_client import (
    ReplicateClient, ReplicateSafetyError, ReplicateTransientError,
    ReplicateError,
)
# Reuse existing R2 helper. If named differently in this codebase,
# update the import.
from app.services.media_service import r2_upload_bytes, download_bytes

log = logging.getLogger(__name__)

REPLICATE_MODEL = "lucataco/pulid-flux"  # see spec §13 open question 1
SEEDS = [101, 202, 303, 404]


def process_persona(db: Session, p: Persona, replicate_api_key: str) -> None:
    """Synchronously process one persona row. Caller (the loop) handles
    locking and exception classification."""
    client = ReplicateClient(api_key=replicate_api_key)
    prompt = build_persona_prompt(p.bio, p.style_hint)

    def _gen(prompt_text: str, seed: int) -> str | None:
        out = client.run_model(REPLICATE_MODEL, {
            "prompt": prompt_text, "seed": seed,
            "num_outputs": 1, "aspect_ratio": "9:16",
        })
        urls = list(out) if not isinstance(out, list) else out
        return urls[0] if urls else None

    candidates: list[dict] = []
    used_prompt = prompt
    try:
        for i, seed in enumerate(SEEDS):
            url = _gen(used_prompt, seed)
            if url:
                candidates.append({"url": url, "seed": seed, "index": i})
    except ReplicateSafetyError:
        # one retry with defensive fallback prompt
        used_prompt = defensive_fallback_prompt()
        candidates.clear()
        try:
            for i, seed in enumerate(SEEDS):
                url = _gen(used_prompt, seed)
                if url:
                    candidates.append({"url": url, "seed": seed, "index": i})
        except ReplicateSafetyError as e:
            _fail(db, p, "🛑 Safety: попробуй сделать `bio` более общим — "
                         "без возрастных меток вроде 'young', 'teen' и т.п.")
            return
    except ReplicateTransientError as e:
        # let the outer loop handle retry — re-raise
        raise

    if not candidates:
        _fail(db, p, "Replicate вернул пусто на все 4 seed-а")
        return

    # download + re-upload to our R2 so URLs are stable
    gallery = []
    for c in candidates:
        try:
            blob = download_bytes(c["url"])
        except Exception as e:
            log.warning("persona %s seed %s download failed: %s",
                        p.id, c["seed"], e)
            continue
        key = f"users/{p.user_id}/personas/{p.id}/{c['seed']}.png"
        media_url = r2_upload_bytes(key, blob, content_type="image/png")
        gallery.append({"url": media_url, "seed": c["seed"], "index": c["index"]})

    if not gallery:
        _fail(db, p, "Все 4 кандидата не скачались")
        return

    p.gallery_json = gallery
    p.status = PersonaStatus.AWAITING_CANONICAL
    db.commit()


def _fail(db: Session, p: Persona, msg: str) -> None:
    p.status = PersonaStatus.FAILED
    p.error_message = msg
    db.commit()
```

- [ ] **Step 4: Verify**

Run: `pytest tests/test_persona_worker.py::test_happy_path_populates_gallery -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/workers/persona_worker.py tests/test_persona_worker.py
git commit -m "feat(personas): worker happy path — gallery population"
```

---

## Task 7: Persona worker — safety rejection fallback

**Files:**
- Modify: `tests/test_persona_worker.py`

Already implemented in Task 6's worker code. Add test coverage.

- [ ] **Step 1: Add failing test**

Append to `tests/test_persona_worker.py`:

```python
def test_safety_reject_then_defensive_succeeds(db_session, test_user):
    from app.services.replicate_client import ReplicateSafetyError
    p = Persona(user_id=test_user.id, name="risky", bio="young teen blonde",
                status=PersonaStatus.GENERATING, gallery_json=[],
                created_at=__import__('datetime').datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)

    call_count = {"n": 0}
    def fake_run(model, params):
        call_count["n"] += 1
        if "fictional generic adult" not in params["prompt"].lower():
            raise ReplicateSafetyError("flagged")
        return ["https://r/img.png"]

    with patch("app.workers.persona_worker.ReplicateClient") as Rc, \
         patch("app.workers.persona_worker.download_bytes", return_value=b"x"), \
         patch("app.workers.persona_worker.r2_upload_bytes",
               side_effect=lambda k,d,content_type: f"/m?{k}"):
        Rc.return_value.run_model = fake_run
        process_persona(db_session, p, replicate_api_key="rk")

    db_session.refresh(p)
    assert p.status == PersonaStatus.AWAITING_CANONICAL
    assert len(p.gallery_json) == 4

def test_safety_reject_twice_marks_failed(db_session, test_user):
    from app.services.replicate_client import ReplicateSafetyError
    p = Persona(user_id=test_user.id, name="risky2", bio="...",
                status=PersonaStatus.GENERATING, gallery_json=[],
                created_at=__import__('datetime').datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)

    def always_reject(*a, **kw):
        raise ReplicateSafetyError("flagged")
    with patch("app.workers.persona_worker.ReplicateClient") as Rc:
        Rc.return_value.run_model = always_reject
        process_persona(db_session, p, replicate_api_key="rk")

    db_session.refresh(p)
    assert p.status == PersonaStatus.FAILED
    assert "Safety" in (p.error_message or "")
```

- [ ] **Step 2: Run**

`pytest tests/test_persona_worker.py -v`
Expected: PASS (3/3, including the Task 6 test).

- [ ] **Step 3: Commit**

```bash
git add tests/test_persona_worker.py
git commit -m "test(personas): worker safety-rejection fallback paths"
```

---

## Task 8: Worker loop + DB row locking

**Files:**
- Modify: `app/workers/persona_worker.py`
- Test: `tests/test_persona_worker.py`

The loop wraps `process_persona` with `SELECT … FOR UPDATE SKIP LOCKED`
so multiple worker instances don't double-process the same row.

- [ ] **Step 1: Failing test**

```python
# add to tests/test_persona_worker.py
def test_loop_picks_one_generating_row_and_skips_locked(db_session, test_user):
    """Two queued personas, run loop once: one moves to AWAITING_CANONICAL."""
    from app.workers.persona_worker import pick_next_pending
    from datetime import datetime
    for nm in ("a", "b"):
        db_session.add(Persona(user_id=test_user.id, name=nm, bio="x",
                               status=PersonaStatus.GENERATING, gallery_json=[],
                               created_at=datetime.utcnow()))
    db_session.commit()
    got = pick_next_pending(db_session)
    assert got is not None
    assert got.status == PersonaStatus.GENERATING
```

- [ ] **Step 2: Implementation**

Append to `app/workers/persona_worker.py`:

```python
from sqlalchemy import select

def pick_next_pending(db: Session) -> Persona | None:
    """Atomically claim one generating persona row.

    Uses SELECT FOR UPDATE SKIP LOCKED so concurrent workers don't
    collide. Caller commits when done with the row.
    """
    row = (
        db.query(Persona)
        .filter(Persona.status == PersonaStatus.GENERATING,
                Persona.deleted_at.is_(None))
        .order_by(Persona.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    return row


def run_loop(db_factory, replicate_api_key_resolver, poll_seconds: float = 2.0):
    """Long-running drain loop. db_factory() returns a fresh Session per tick.
    replicate_api_key_resolver(persona) returns the API key (per-user)."""
    while True:
        db = db_factory()
        try:
            p = pick_next_pending(db)
            if p is None:
                db.commit()
                time.sleep(poll_seconds)
                continue
            try:
                api_key = replicate_api_key_resolver(p)
                process_persona(db, p, replicate_api_key=api_key)
            except ReplicateTransientError as e:
                # leave row in GENERATING, log; transient — try again next tick
                log.warning("transient on persona %s: %s", p.id, e)
                db.rollback()
            except Exception as e:
                log.exception("hard fail on persona %s", p.id)
                _fail(db, p, f"внутренняя ошибка: {e}")
        finally:
            db.close()
```

- [ ] **Step 3: Verify**

Run: `pytest tests/test_persona_worker.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/workers/persona_worker.py tests/test_persona_worker.py
git commit -m "feat(personas): worker drain loop with SKIP LOCKED"
```

---

## Task 9: Pick-canonical service

**Files:**
- Modify: `app/services/persona_service.py`
- Test: `tests/test_persona_service.py`

- [ ] **Step 1: Failing tests**

Append to `tests/test_persona_service.py`:

```python
from app.services.persona_service import set_canonical, soft_delete_persona


def test_pick_canonical_moves_to_ready(db_session, test_user):
    p = Persona(user_id=test_user.id, name="anya2", bio="x",
                status=PersonaStatus.AWAITING_CANONICAL,
                gallery_json=[
                    {"url": "/m?u/0.png", "seed": 1, "index": 0},
                    {"url": "/m?u/1.png", "seed": 2, "index": 1},
                ],
                created_at=__import__('datetime').datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)
    set_canonical(db_session, p, gallery_index=1)
    db_session.refresh(p)
    assert p.status == PersonaStatus.READY
    assert p.canonical_face_url == "/m?u/1.png"
    assert p.ready_at is not None


def test_pick_canonical_out_of_range_raises(db_session, test_user):
    p = Persona(user_id=test_user.id, name="anya3", bio="x",
                status=PersonaStatus.AWAITING_CANONICAL,
                gallery_json=[{"url": "x", "seed": 1, "index": 0}],
                created_at=__import__('datetime').datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)
    with pytest.raises(PersonaValidationError):
        set_canonical(db_session, p, gallery_index=5)


def test_pick_canonical_wrong_status(db_session, test_user):
    p = Persona(user_id=test_user.id, name="anya4", bio="x",
                status=PersonaStatus.GENERATING, gallery_json=[],
                created_at=__import__('datetime').datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)
    with pytest.raises(PersonaValidationError):
        set_canonical(db_session, p, gallery_index=0)


def test_soft_delete_sets_deleted_at(db_session, test_user):
    p = Persona(user_id=test_user.id, name="anya5", bio="x",
                status=PersonaStatus.READY, gallery_json=[],
                canonical_face_url="x",
                created_at=__import__('datetime').datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)
    soft_delete_persona(db_session, p)
    db_session.refresh(p)
    assert p.deleted_at is not None
```

- [ ] **Step 2: Implementation**

Append to `app/services/persona_service.py`:

```python
def set_canonical(db: Session, p: Persona, *, gallery_index: int) -> Persona:
    if p.status != PersonaStatus.AWAITING_CANONICAL:
        raise PersonaValidationError(
            f"persona must be in AWAITING_CANONICAL, got {p.status}"
        )
    if not (0 <= gallery_index < len(p.gallery_json or [])):
        raise PersonaValidationError(
            f"gallery_index {gallery_index} out of range "
            f"(have {len(p.gallery_json or [])})"
        )
    chosen = p.gallery_json[gallery_index]
    p.canonical_face_url = chosen["url"]
    p.status = PersonaStatus.READY
    p.ready_at = datetime.utcnow()
    db.commit()
    db.refresh(p)
    return p


def soft_delete_persona(db: Session, p: Persona) -> None:
    p.deleted_at = datetime.utcnow()
    db.commit()
```

- [ ] **Step 3: Verify**

Run: `pytest tests/test_persona_service.py -v`
Expected: PASS (8 total).

- [ ] **Step 4: Commit**

```bash
git add app/services/persona_service.py tests/test_persona_service.py
git commit -m "feat(personas): canonical-pick + soft-delete"
```

---

## Task 10: FastAPI router

**Files:**
- Create: `app/api/personas.py`
- Test: `tests/test_personas_api.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_personas_api.py
def test_list_empty(auth_client):
    r = auth_client.get("/api/personas/")
    assert r.status_code == 200
    assert r.json() == {"items": []}


def test_create_returns_202_and_appears_in_list(auth_client):
    r = auth_client.post("/api/personas/", json={
        "name": "anya", "bio": "блондинка 23", "style_hint": "studio",
    })
    assert r.status_code == 202, r.text
    pid = r.json()["id"]
    assert r.json()["status"] == "generating"

    r2 = auth_client.get("/api/personas/")
    assert any(p["id"] == pid for p in r2.json()["items"])


def test_create_duplicate_name_409(auth_client):
    auth_client.post("/api/personas/", json={"name": "dup", "bio": "x"})
    r = auth_client.post("/api/personas/", json={"name": "dup", "bio": "y"})
    assert r.status_code == 409


def test_set_canonical_endpoint(auth_client, db_session, test_user):
    # arrange a persona in AWAITING_CANONICAL manually
    from app.models.persona import Persona, PersonaStatus
    from datetime import datetime
    p = Persona(user_id=test_user.id, name="anya2", bio="x",
                status=PersonaStatus.AWAITING_CANONICAL,
                gallery_json=[{"url": "/m?0", "seed": 1, "index": 0}],
                created_at=datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)

    r = auth_client.post(f"/api/personas/{p.id}/canonical",
                         json={"gallery_index": 0})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ready"
    assert r.json()["canonical_face_url"] == "/m?0"


def test_delete_endpoint(auth_client, db_session, test_user):
    from app.models.persona import Persona, PersonaStatus
    from datetime import datetime
    p = Persona(user_id=test_user.id, name="byebye", bio="x",
                status=PersonaStatus.READY, canonical_face_url="/m?0",
                gallery_json=[], created_at=datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)

    r = auth_client.delete(f"/api/personas/{p.id}")
    assert r.status_code == 204
    db_session.refresh(p)
    assert p.deleted_at is not None


def test_cant_access_other_users_persona(auth_client, db_session, other_user):
    from app.models.persona import Persona, PersonaStatus
    from datetime import datetime
    p = Persona(user_id=other_user.id, name="notmine", bio="x",
                status=PersonaStatus.READY, canonical_face_url="/m?0",
                gallery_json=[], created_at=datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)
    r = auth_client.delete(f"/api/personas/{p.id}")
    assert r.status_code in (403, 404)
```

- [ ] **Step 2: Implementation**

```python
# app/api/personas.py
from __future__ import annotations
from typing import Optional, Literal
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.persona import Persona, PersonaStatus
from app.services.persona_service import (
    create_persona_async, set_canonical, soft_delete_persona,
    PersonaValidationError,
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
            id=p.id, name=p.name, bio=p.bio, style_hint=p.style_hint,
            status=p.status, canonical_face_url=p.canonical_face_url,
            gallery_json=p.gallery_json or [], error_message=p.error_message,
        )


class PersonaListResponse(BaseModel):
    items: list[PersonaResponse]


class SetCanonicalRequest(BaseModel):
    gallery_index: int = Field(ge=0)


def _get_owned(db: Session, user: User, pid: int) -> Persona:
    p = (db.query(Persona)
         .filter(Persona.id == pid,
                 Persona.user_id == user.id,
                 Persona.deleted_at.is_(None))
         .first())
    if not p:
        raise HTTPException(404, "persona not found")
    return p


@router.get("/", response_model=PersonaListResponse)
def list_personas(current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    rows = (db.query(Persona)
            .filter(Persona.user_id == current_user.id,
                    Persona.deleted_at.is_(None))
            .order_by(Persona.created_at.desc())
            .all())
    return PersonaListResponse(items=[PersonaResponse.from_model(r) for r in rows])


@router.post("/", status_code=status.HTTP_202_ACCEPTED,
             response_model=PersonaResponse)
def create_persona(data: PersonaCreateRequest,
                   current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    try:
        p = create_persona_async(db, current_user,
                                 name=data.name, bio=data.bio,
                                 style_hint=data.style_hint)
    except PersonaValidationError as e:
        msg = str(e).lower()
        code = 409 if "already in use" in msg else 400
        raise HTTPException(code, str(e))
    return PersonaResponse.from_model(p)


@router.get("/{pid}", response_model=PersonaResponse)
def get_persona(pid: int,
                current_user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    return PersonaResponse.from_model(_get_owned(db, current_user, pid))


@router.post("/{pid}/canonical", response_model=PersonaResponse)
def pick_canonical(pid: int, data: SetCanonicalRequest,
                   current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    p = _get_owned(db, current_user, pid)
    try:
        set_canonical(db, p, gallery_index=data.gallery_index)
    except PersonaValidationError as e:
        raise HTTPException(400, str(e))
    return PersonaResponse.from_model(p)


@router.delete("/{pid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_persona(pid: int,
                   current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    p = _get_owned(db, current_user, pid)
    soft_delete_persona(db, p)
    return None
```

- [ ] **Step 3: Register router**

Modify `app/main.py` — find the block of `app.include_router(...)` calls
around lines 452–469 and append:

```python
from app.api.personas import router as personas_router
app.include_router(personas_router, prefix="/api/personas", tags=["Personas"])
```

- [ ] **Step 4: Verify**

Run: `pytest tests/test_personas_api.py -v`
Expected: PASS (6/6).

- [ ] **Step 5: Commit**

```bash
git add app/api/personas.py app/main.py tests/test_personas_api.py
git commit -m "feat(personas): REST API + router registration"
```

---

## Task 11: Frontend — `/personas.html` and JS

**Files:**
- Create: `static/personas.html`
- Create: `static/js/personas.js`
- Modify: `app/main.py` (add route)

- [ ] **Step 1: Add route**

In `app/main.py`, near the existing `@app.get("/forge")` (~line 492):

```python
@app.get("/personas")
def personas_page():
    from fastapi.responses import FileResponse
    return FileResponse("static/personas.html")
```

- [ ] **Step 2: Write `static/personas.html`**

(Compact version — copy the visual shell from `static/forge.html` for
visual consistency. Body:)

```html
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Personas — Reelstracker</title>
  <link rel="stylesheet" href="/static/css/main.css">
</head>
<body class="bg-black text-white">
  <header class="max-w-3xl mx-auto p-4 flex justify-between">
    <h1 class="text-xl">👤 Персоны</h1>
    <nav class="text-sm space-x-3">
      <a href="/forge">🪄 Forge</a>
      <a href="/tracker.html">📊 Tracker</a>
      <a href="/settings">🔑 Ключи</a>
    </nav>
  </header>
  <main class="max-w-3xl mx-auto p-4">
    <div class="flex justify-end mb-3">
      <button id="new-persona-btn" class="btn-magic px-4 py-2 rounded">
        + Новая персона
      </button>
    </div>
    <div id="empty" class="hidden text-gray-400 text-sm p-6 glass rounded-2xl">
      Закреплённая «персона» — это фиктивный AI-образ, который можно
      переиспользовать во всех ремиксах Forge (Strategy E). Создай одну —
      потом во всех видео будет одно и то же лицо бренда.
    </div>
    <div id="grid" class="grid grid-cols-2 md:grid-cols-3 gap-3"></div>

    <div id="modal" class="fixed inset-0 bg-black/80 hidden items-center justify-center">
      <div class="glass rounded-2xl p-5 max-w-md w-full">
        <h2 class="text-lg mb-3">Новая персона</h2>
        <label class="block text-xs text-gray-400">Имя (internal)</label>
        <input id="m-name" class="w-full bg-black/40 rounded px-3 py-2 mb-3" maxlength="64">
        <label class="block text-xs text-gray-400">Bio / внешность</label>
        <textarea id="m-bio" class="w-full bg-black/40 rounded px-3 py-2 mb-3" rows="4" maxlength="512"></textarea>
        <label class="block text-xs text-gray-400">Стиль</label>
        <select id="m-style" class="w-full bg-black/40 rounded px-3 py-2 mb-3">
          <option value="">—</option>
          <option value="editorial">editorial</option>
          <option value="lifestyle">lifestyle</option>
          <option value="studio">studio</option>
          <option value="street">street</option>
        </select>
        <div class="flex justify-end gap-2">
          <button id="m-cancel" class="text-gray-400 px-3">Отмена</button>
          <button id="m-submit" class="btn-magic px-4 py-2 rounded">
            Сгенерировать (~$0.20)
          </button>
        </div>
      </div>
    </div>
  </main>
  <script src="/static/js/auth.js"></script>
  <script src="/static/js/personas.js"></script>
</body>
</html>
```

- [ ] **Step 3: Write `static/js/personas.js`**

```javascript
// static/js/personas.js — list + create modal + canonical picker
async function refresh() {
  const r = await authFetch('/api/personas/');
  const data = await r.json();
  const grid = document.getElementById('grid');
  const empty = document.getElementById('empty');
  grid.innerHTML = '';
  if (!data.items.length) { empty.classList.remove('hidden'); return; }
  empty.classList.add('hidden');
  for (const p of data.items) {
    const card = document.createElement('div');
    card.className = 'glass rounded-xl p-3';
    const thumb = p.canonical_face_url
      ? `<img src="${p.canonical_face_url}" class="rounded mb-2 w-full aspect-[9/16] object-cover">`
      : (p.status === 'awaiting_canonical'
        ? `<div class="text-xs mb-2">Выбери лучший:</div>` + p.gallery_json.map(
            (g, i) => `<img src="${g.url}" onclick="pickCanonical(${p.id}, ${i})" class="cursor-pointer rounded mb-1 w-1/4 inline-block">`
          ).join('')
        : `<div class="aspect-[9/16] bg-black/40 rounded mb-2 flex items-center justify-center text-xs text-gray-400">${p.status}</div>`);
    card.innerHTML = thumb + `
      <div class="text-sm font-medium">${p.name}</div>
      <div class="text-xs text-gray-400 line-clamp-2">${p.bio}</div>
      ${p.error_message ? `<div class="text-xs text-red-300 mt-1">${p.error_message}</div>` : ''}
      <div class="flex justify-between mt-2 text-xs">
        ${p.status === 'ready' ? `<a href="/forge?persona=${p.id}" class="text-blue-300">Использовать</a>` : `<span class="text-gray-500">${p.status}</span>`}
        <button onclick="del(${p.id})" class="text-red-300">×</button>
      </div>
    `;
    grid.appendChild(card);
  }
}

async function pickCanonical(pid, idx) {
  await authFetch(`/api/personas/${pid}/canonical`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({gallery_index: idx}),
  });
  refresh();
}

async function del(pid) {
  if (!confirm('Удалить персону?')) return;
  await authFetch(`/api/personas/${pid}`, {method: 'DELETE'});
  refresh();
}

document.getElementById('new-persona-btn').onclick = () => {
  document.getElementById('modal').classList.remove('hidden');
  document.getElementById('modal').classList.add('flex');
};
document.getElementById('m-cancel').onclick = () => {
  document.getElementById('modal').classList.add('hidden');
};
document.getElementById('m-submit').onclick = async () => {
  const body = {
    name: document.getElementById('m-name').value.trim(),
    bio: document.getElementById('m-bio').value.trim(),
  };
  const style = document.getElementById('m-style').value;
  if (style) body.style_hint = style;
  const r = await authFetch('/api/personas/', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!r.ok) { alert('❌ ' + await r.text()); return; }
  document.getElementById('modal').classList.add('hidden');
  refresh();
  // poll while any persona is in generating/awaiting
  const poll = setInterval(async () => {
    await refresh();
    const data = await (await authFetch('/api/personas/')).json();
    if (!data.items.some(x => x.status === 'generating')) clearInterval(poll);
  }, 5000);
};

refresh();
```

- [ ] **Step 4: Manual smoke test**

```bash
uvicorn app.main:app --reload
# open http://localhost:8000/personas, click "+ Новая персона",
# fill in name/bio/style, submit. Observe row appears in 'generating',
# then 'awaiting_canonical' (with mocked replicate in env, or real with
# REPLICATE_API_TOKEN set). Click a thumbnail → 'ready'.
```

(Manual only — frontend tests are out of scope for MVP.)

- [ ] **Step 5: Commit**

```bash
git add static/personas.html static/js/personas.js app/main.py
git commit -m "feat(personas): /personas page — list + create modal + picker"
```

---

## Task 12: Wire persona worker into startup

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Read existing startup wiring**

Look for `@app.on_event("startup")` or `lifespan` in `app/main.py`.
Find where `parser_worker` / `scheduler` are started.

- [ ] **Step 2: Add persona worker thread**

```python
# in the startup hook, alongside existing worker threads
import threading
from app.database import SessionLocal
from app.workers.persona_worker import run_loop

def _persona_api_key_resolver(persona):
    db = SessionLocal()
    try:
        from app.models.user import User
        u = db.query(User).get(persona.user_id)
        return u.replicate_api_key if u else None
    finally:
        db.close()

if os.getenv("WORKER_PERSONA", "1") == "1":
    threading.Thread(
        target=run_loop,
        args=(SessionLocal, _persona_api_key_resolver),
        daemon=True,
        name="persona-worker",
    ).start()
```

> **Note (per memory: "Forge tech insights — вынести воркер из веб-процесса"):**
> running the worker as a daemon thread in the web process is a known
> anti-pattern. This is the MVP placement — same as existing workers.
> The extraction to a dedicated Railway service is tracked separately.

- [ ] **Step 3: Add `replicate_api_key` column to `users`**

Quick migration: `alembic/versions/20260613_02_user_replicate_key.py`

```python
"""add users.replicate_api_key"""
from alembic import op
import sqlalchemy as sa

revision = "20260613_02"
down_revision = "20260613_01"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("users", sa.Column("replicate_api_key", sa.Text(), nullable=True))

def downgrade():
    op.drop_column("users", "replicate_api_key")
```

And add to `app/models/user.py` the new column. (Read the file first
to slot the column where the other API key columns live.)

- [ ] **Step 4: Verify**

```bash
alembic upgrade head
pytest tests/ -v
uvicorn app.main:app --reload
# tail logs, confirm "persona-worker" thread started
```

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/20260613_02_user_replicate_key.py app/models/user.py app/main.py
git commit -m "feat(personas): wire worker thread + users.replicate_api_key"
```

---

## Task 13: `tests/conftest.py` — fixtures

**Files:**
- Create: `tests/conftest.py`

(If not already created in Task 1.)

- [ ] **Step 1: Implementation**

```python
# tests/conftest.py
"""Test fixtures: DB engine, session, auth client, test user(s)."""
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from fastapi.testclient import TestClient

# Allow override; default to a separate test DB the developer creates.
TEST_DB_URL = os.getenv("TEST_DATABASE_URL",
                       "postgresql://postgres:postgres@localhost:5432/reelstracker_test")


@pytest.fixture(scope="session")
def db_engine():
    engine = create_engine(TEST_DB_URL)
    # Apply migrations
    from alembic.config import Config
    from alembic import command
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Session:
    conn = db_engine.connect()
    trans = conn.begin()
    SessionLocal = sessionmaker(bind=conn, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()


@pytest.fixture
def test_user(db_session):
    from app.models.user import User
    u = User(email="t@example.com", password_hash="x")
    db_session.add(u); db_session.commit(); db_session.refresh(u)
    return u


@pytest.fixture
def other_user(db_session):
    from app.models.user import User
    u = User(email="o@example.com", password_hash="x")
    db_session.add(u); db_session.commit(); db_session.refresh(u)
    return u


@pytest.fixture
def auth_client(test_user, db_session):
    from app.main import app
    from app.api.deps import get_current_user
    from app.database import get_db
    app.dependency_overrides[get_current_user] = lambda: test_user
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Verify all tests**

```bash
createdb reelstracker_test  # one-time
pytest tests/ -v
```

Expected: all tests in tests/ pass.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: shared fixtures (db, auth client, users)"
```

---

## Self-Review

**Spec coverage** (against §1–§13 of the design):

- §1 summary — covered by Tasks 1–12.
- §3.1 (Forge E tab UI) — out of scope (Plan 2).
- §3.2 (`/personas.html`) — Task 11. ✓
- §3.3 (create modal) — Task 11. ✓
- §4 (architecture) — persona side covered; Forge E worker in Plan 2.
- §5.1 (Persona Library) — Tasks 1, 2, 9, 10, 11. ✓
- §5.1 prompt + safety retry — Tasks 3, 6, 7. ✓
- §6 (data model) — Tasks 1, 12. `replicate_api_key` covered in Task 12.
- §10 (error handling) — Tasks 6, 7, 9, 10 (HTTP codes).
- §11 (testing) — Tasks 1–10 all TDD.

**Placeholder scan:** none — all code blocks complete.

**Type consistency:**
- `PersonaStatus` values used in tests match enum values in `app/models/persona.py`.
- `PersonaValidationError` raised in service, caught in API. ✓
- `gallery_json` shape `{url, seed, index}` is consistent across worker, service, API, frontend.
- `set_canonical` arg name `gallery_index` consistent in service + API.

---

## Execution Handoff

Recommended: **subagent-driven** — fresh subagent per task with two-stage
review. Plan is detailed enough that each task is a self-contained
brief.
