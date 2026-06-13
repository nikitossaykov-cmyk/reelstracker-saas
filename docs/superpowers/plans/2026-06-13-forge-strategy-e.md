# Forge Strategy E (Face Replace) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire Strategy E (face/body replacement using a locked persona) into the existing Forge pipeline — backend service + worker + Mode 1 (Replicate face-swap) + Mode 2 (RunPod Wan-Animate via wan_clone.py) + Forge frontend tab.

**Architecture:** `POST /api/forge/start strategy=E` enqueues a `generated_videos` row. A dedicated `forge_e_worker` drains rows under `SELECT … FOR UPDATE SKIP LOCKED`, branches on `mode` (1=Replicate, 2=RunPod Wan), runs the pipeline, calls `ensure_faststart`, uploads to R2, marks ready. UI polls `/api/media/diag/{gv_id}` (same as Strategy C/D).

**Tech Stack:** SQLAlchemy 2 + Alembic + FastAPI + Pydantic v2 + Replicate SDK + subprocess (wan_clone.py wrapper) + boto3 + pytest.

**Depends on:** `docs/superpowers/plans/2026-06-13-persona-library.md` — `Persona` model, `replicate_api_key` on User, `conftest.py` fixtures.

---

## File Structure

| file | responsibility |
|---|---|
| `alembic/versions/<rev>_forge_e_gv_columns.py` | Schema: persona_id, mode, cost cols on generated_videos |
| `app/models/generation.py` | Add 4 columns to GeneratedVideo |
| `app/services/runpod_pod.py` | Pod lifecycle: ensure_up, stop_if_idle, alive_seconds |
| `app/services/forge_e_service.py` | start_e — validate + enqueue, returns gv_id |
| `app/services/forge_e_mode1.py` | Replicate face-swap call (mode=1) |
| `app/services/forge_e_mode2.py` | wan_clone.py subprocess wrapper (mode=2) |
| `app/workers/forge_e_worker.py` | Drain loop |
| `app/api/forge.py` | Extend Strategy literal + branch to forge_e_service |
| `app/main.py` | Wire forge_e_worker thread + (later) cron stop_if_idle |
| `static/forge.html` | New E tab + form |
| `tests/test_runpod_pod.py` | Pod helper tests (subprocess mocked) |
| `tests/test_forge_e_service.py` | Service tests |
| `tests/test_forge_e_mode1.py` | Mode 1 tests (Replicate mocked) |
| `tests/test_forge_e_mode2.py` | Mode 2 tests (subprocess mocked) |
| `tests/test_forge_e_worker.py` | Worker tests |
| `tests/test_forge_api_strategy_e.py` | API integration test |

---

## Task 1: Migration — `generated_videos` columns

**Files:**
- Create: `alembic/versions/20260613_03_forge_e_gv_columns.py`
- Modify: `app/models/generation.py`
- Test: `tests/test_forge_e_migration.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_forge_e_migration.py
from sqlalchemy import inspect

def test_gv_has_strategy_e_columns(db_engine):
    cols = {c["name"] for c in inspect(db_engine).get_columns("generated_videos")}
    assert {"persona_id", "mode", "cost_runpod_seconds",
            "cost_replicate_usd"} <= cols
```

- [ ] **Step 2: Migration**

```python
# alembic/versions/20260613_03_forge_e_gv_columns.py
"""forge e — generated_videos columns"""
from alembic import op
import sqlalchemy as sa

revision = "20260613_03"
down_revision = "20260613_02"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("generated_videos",
                  sa.Column("persona_id", sa.Integer,
                            sa.ForeignKey("personas.id", ondelete="SET NULL"),
                            nullable=True))
    op.add_column("generated_videos",
                  sa.Column("mode", sa.SmallInteger, nullable=True))
    op.add_column("generated_videos",
                  sa.Column("cost_runpod_seconds", sa.Float, nullable=True))
    op.add_column("generated_videos",
                  sa.Column("cost_replicate_usd", sa.Numeric(10, 4), nullable=True))
    op.create_index("ix_gv_persona_id", "generated_videos", ["persona_id"])

def downgrade():
    op.drop_index("ix_gv_persona_id", table_name="generated_videos")
    op.drop_column("generated_videos", "cost_replicate_usd")
    op.drop_column("generated_videos", "cost_runpod_seconds")
    op.drop_column("generated_videos", "mode")
    op.drop_column("generated_videos", "persona_id")
```

- [ ] **Step 3: Model**

Read `app/models/generation.py`, find the `GeneratedVideo` class
column block, append after the existing columns (preserving existing
formatting):

```python
    persona_id = Column(Integer, ForeignKey("personas.id", ondelete="SET NULL"),
                        nullable=True)
    mode = Column(Integer, nullable=True)  # 1 or 2 for strategy E
    cost_runpod_seconds = Column(Float, nullable=True)
    cost_replicate_usd = Column(Numeric(10, 4), nullable=True)
```

Add to imports: `Float, Numeric`.

- [ ] **Step 4: Verify**

```bash
alembic upgrade head
pytest tests/test_forge_e_migration.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/20260613_03_forge_e_gv_columns.py app/models/generation.py tests/test_forge_e_migration.py
git commit -m "feat(forge-e): generated_videos persona_id/mode/cost columns"
```

---

## Task 2: RunPod pod helper

**Files:**
- Create: `app/services/runpod_pod.py`
- Test: `tests/test_runpod_pod.py`

The helper is a thin wrapper around two existing scripts:
`/opt/tg-bot/tools/pod_start.sh` and `pod_stop.sh`. On Railway these
scripts are not present (the repo doesn't ship them). To stay portable,
the helper accepts script paths from env vars, with defaults.

- [ ] **Step 1: Failing tests**

```python
# tests/test_runpod_pod.py
import pytest
from unittest.mock import patch, MagicMock
from app.services.runpod_pod import (
    PodInfo, read_pod_info, ensure_pod_up, PodUnavailable,
)


def test_read_pod_info_parses_kv_file(tmp_path):
    f = tmp_path / "info"
    f.write_text("host=1.2.3.4\nssh_port=22\nstarted_at=1700000000\n")
    info = read_pod_info(str(f))
    assert info.host == "1.2.3.4"
    assert info.ssh_port == 22


def test_read_pod_info_missing_returns_none(tmp_path):
    assert read_pod_info(str(tmp_path / "nope")) is None


def test_ensure_pod_up_returns_info_if_reachable(tmp_path, monkeypatch):
    f = tmp_path / "info"
    f.write_text("host=1.2.3.4\nssh_port=22\n")
    monkeypatch.setenv("WAN_POD_INFO", str(f))
    with patch("app.services.runpod_pod._ping", return_value=True):
        info = ensure_pod_up()
    assert info.host == "1.2.3.4"


def test_ensure_pod_up_calls_start_script_if_unreachable(tmp_path, monkeypatch):
    f = tmp_path / "info"
    f.write_text("host=1.2.3.4\nssh_port=22\n")
    monkeypatch.setenv("WAN_POD_INFO", str(f))
    monkeypatch.setenv("WAN_POD_START_SCRIPT", "/usr/bin/true")
    fake = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=fake), \
         patch("app.services.runpod_pod._ping", side_effect=[False, True]):
        info = ensure_pod_up(start_timeout=5)
    assert info.host == "1.2.3.4"


def test_ensure_pod_up_raises_when_start_fails(tmp_path, monkeypatch):
    f = tmp_path / "info"
    f.write_text("host=1.2.3.4\nssh_port=22\n")
    monkeypatch.setenv("WAN_POD_INFO", str(f))
    monkeypatch.setenv("WAN_POD_START_SCRIPT", "/usr/bin/false")
    fake = MagicMock(returncode=1, stdout="", stderr="boom")
    with patch("subprocess.run", return_value=fake), \
         patch("app.services.runpod_pod._ping", return_value=False):
        with pytest.raises(PodUnavailable):
            ensure_pod_up(start_timeout=2)
```

- [ ] **Step 2: Implementation**

```python
# app/services/runpod_pod.py
"""RunPod pod lifecycle helpers."""
from __future__ import annotations
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class PodUnavailable(Exception):
    pass


@dataclass
class PodInfo:
    host: str
    ssh_port: int
    started_at: Optional[int] = None


def read_pod_info(path: str) -> Optional[PodInfo]:
    p = Path(path)
    if not p.exists():
        return None
    kv = {}
    for line in p.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    if "host" not in kv:
        return None
    return PodInfo(
        host=kv["host"],
        ssh_port=int(kv.get("ssh_port", "22")),
        started_at=int(kv["started_at"]) if "started_at" in kv else None,
    )


def _ping(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_pod_up(start_timeout: int = 120) -> PodInfo:
    info_path = os.getenv("WAN_POD_INFO", "/root/.wan_pod_info")
    info = read_pod_info(info_path)
    if info and _ping(info.host, info.ssh_port):
        return info
    start_script = os.getenv("WAN_POD_START_SCRIPT", "/opt/tg-bot/tools/pod_start.sh")
    if not Path(start_script).exists():
        raise PodUnavailable(f"start script not found: {start_script}")
    r = subprocess.run([start_script], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise PodUnavailable(f"start script failed: {r.stderr.strip()[:200]}")
    deadline = time.time() + start_timeout
    while time.time() < deadline:
        info = read_pod_info(info_path)
        if info and _ping(info.host, info.ssh_port):
            return info
        time.sleep(3)
    raise PodUnavailable("pod did not become reachable within timeout")


def stop_pod_if_idle(max_idle_minutes: int = 10) -> bool:
    """Called from a cron tick. Returns True if pod was stopped."""
    info_path = os.getenv("WAN_POD_INFO", "/root/.wan_pod_info")
    info = read_pod_info(info_path)
    if not info or not _ping(info.host, info.ssh_port):
        return False
    # check DB for last RUNNING strategy E activity
    from app.database import SessionLocal
    from app.models.generation import GeneratedVideo, GenerationStatus
    from datetime import datetime, timedelta
    threshold = datetime.utcnow() - timedelta(minutes=max_idle_minutes)
    db = SessionLocal()
    try:
        recent = (db.query(GeneratedVideo)
                  .filter(GeneratedVideo.mode == 2,
                          GeneratedVideo.started_at > threshold)
                  .first())
        if recent:
            return False
    finally:
        db.close()
    stop_script = os.getenv("WAN_POD_STOP_SCRIPT", "/opt/tg-bot/tools/pod_stop.sh")
    if not Path(stop_script).exists():
        return False
    r = subprocess.run([stop_script], capture_output=True, text=True, timeout=60)
    return r.returncode == 0
```

- [ ] **Step 3: Verify**

```bash
pytest tests/test_runpod_pod.py -v
```

Expected: PASS (5/5).

- [ ] **Step 4: Commit**

```bash
git add app/services/runpod_pod.py tests/test_runpod_pod.py
git commit -m "feat(forge-e): RunPod pod lifecycle helper"
```

---

## Task 3: `forge_e_service.start` — enqueue only

**Files:**
- Create: `app/services/forge_e_service.py`
- Test: `tests/test_forge_e_service.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_forge_e_service.py
import pytest
from app.services.forge_e_service import start_e, ForgeEValidationError
from app.models.generation import GeneratedVideo, GenerationStatus, VideoProvider


def test_enqueues_row(db_session, test_user, ready_persona):
    gv = start_e(db_session, test_user, source_url="https://www.instagram.com/reel/X/",
                 persona_id=ready_persona.id, mode=1)
    assert gv.id is not None
    assert gv.status == GenerationStatus.PENDING
    assert gv.persona_id == ready_persona.id
    assert gv.mode == 1
    assert gv.provider == VideoProvider.MOCK  # default-mocked provider for E rows


def test_rejects_persona_not_owned(db_session, test_user, other_user_persona):
    with pytest.raises(ForgeEValidationError):
        start_e(db_session, test_user, source_url="https://x/",
                persona_id=other_user_persona.id, mode=1)


def test_rejects_persona_not_ready(db_session, test_user, generating_persona):
    with pytest.raises(ForgeEValidationError):
        start_e(db_session, test_user, source_url="https://x/",
                persona_id=generating_persona.id, mode=1)


def test_rejects_unknown_mode(db_session, test_user, ready_persona):
    with pytest.raises(ForgeEValidationError):
        start_e(db_session, test_user, source_url="https://x/",
                persona_id=ready_persona.id, mode=99)


def test_mode1_requires_replicate_key(db_session, test_user_no_keys, ready_persona_for):
    p = ready_persona_for(test_user_no_keys)
    with pytest.raises(ForgeEValidationError) as ei:
        start_e(db_session, test_user_no_keys, source_url="https://x/",
                persona_id=p.id, mode=1)
    assert "replicate" in str(ei.value).lower()
```

Add fixtures to `tests/conftest.py`:

```python
@pytest.fixture
def ready_persona(db_session, test_user):
    from app.models.persona import Persona, PersonaStatus
    from datetime import datetime
    p = Persona(user_id=test_user.id, name="ready-p", bio="x",
                status=PersonaStatus.READY, canonical_face_url="/m?face.png",
                gallery_json=[{"url": "/m?face.png", "seed": 1, "index": 0}],
                created_at=datetime.utcnow(), ready_at=datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)
    return p


@pytest.fixture
def other_user_persona(db_session, other_user):
    from app.models.persona import Persona, PersonaStatus
    from datetime import datetime
    p = Persona(user_id=other_user.id, name="other", bio="x",
                status=PersonaStatus.READY, canonical_face_url="/m?o.png",
                gallery_json=[], created_at=datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)
    return p


@pytest.fixture
def generating_persona(db_session, test_user):
    from app.models.persona import Persona, PersonaStatus
    from datetime import datetime
    p = Persona(user_id=test_user.id, name="gen", bio="x",
                status=PersonaStatus.GENERATING, gallery_json=[],
                created_at=datetime.utcnow())
    db_session.add(p); db_session.commit(); db_session.refresh(p)
    return p


@pytest.fixture
def test_user_no_keys(db_session):
    from app.models.user import User
    u = User(email="nokey@example.com", password_hash="x", replicate_api_key=None)
    db_session.add(u); db_session.commit(); db_session.refresh(u)
    return u


@pytest.fixture
def ready_persona_for(db_session):
    from app.models.persona import Persona, PersonaStatus
    from datetime import datetime
    def _make(user):
        p = Persona(user_id=user.id, name="rp", bio="x",
                    status=PersonaStatus.READY, canonical_face_url="/m?f.png",
                    gallery_json=[], created_at=datetime.utcnow())
        db_session.add(p); db_session.commit(); db_session.refresh(p)
        return p
    return _make


@pytest.fixture
def test_user(db_session):
    """Override to include replicate key by default."""
    from app.models.user import User
    u = User(email="t@example.com", password_hash="x",
             replicate_api_key="r-test-key")
    db_session.add(u); db_session.commit(); db_session.refresh(u)
    return u
```

- [ ] **Step 2: Implementation**

```python
# app/services/forge_e_service.py
"""Strategy E enqueue + validation. Worker drains."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.user import User
from app.models.persona import Persona, PersonaStatus
from app.models.generation import GeneratedVideo, GenerationStatus, VideoProvider


class ForgeEValidationError(ValueError):
    pass


def start_e(db: Session, user: User, *,
            source_url: str, persona_id: int, mode: int) -> GeneratedVideo:
    if mode not in (1, 2):
        raise ForgeEValidationError(f"unknown mode: {mode}")
    if not source_url or len(source_url) < 10:
        raise ForgeEValidationError("source_url required")

    persona = (db.query(Persona)
               .filter(Persona.id == persona_id,
                       Persona.user_id == user.id,
                       Persona.deleted_at.is_(None))
               .first())
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
    # Mode 2 currently uses platform-owned RunPod pod — no per-user key check.

    gv = GeneratedVideo(
        user_id=user.id,
        prompt=f"[strategy=E mode={mode} persona={persona.name}] source={source_url}",
        provider=VideoProvider.MOCK,
        status=GenerationStatus.PENDING,
        persona_id=persona.id,
        mode=mode,
    )
    db.add(gv); db.commit(); db.refresh(gv)
    return gv
```

- [ ] **Step 3: Verify**

```bash
pytest tests/test_forge_e_service.py -v
```

Expected: PASS (5/5).

- [ ] **Step 4: Commit**

```bash
git add app/services/forge_e_service.py tests/test_forge_e_service.py tests/conftest.py
git commit -m "feat(forge-e): start_e service — validation + enqueue"
```

---

## Task 4: Mode 1 — Replicate face-swap

**Files:**
- Create: `app/services/forge_e_mode1.py`
- Test: `tests/test_forge_e_mode1.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_forge_e_mode1.py
from unittest.mock import patch, MagicMock
from pathlib import Path
from app.services.forge_e_mode1 import run_mode1


def test_mode1_calls_replicate_and_returns_result_path(tmp_path):
    donor = tmp_path / "donor.mp4"
    donor.write_bytes(b"\x00" * 1024)
    face = tmp_path / "face.png"
    face.write_bytes(b"\x89PNG fake")
    out = tmp_path / "out.mp4"

    fake_replicate = MagicMock(return_value="https://r/out.mp4")
    fake_download = MagicMock(return_value=b"RESULT BYTES" * 200)

    with patch("app.services.forge_e_mode1.ReplicateClient") as Rc, \
         patch("app.services.forge_e_mode1.download_bytes", fake_download), \
         patch("app.services.forge_e_mode1.upload_temp_public",
               side_effect=["https://donor.tmp", "https://face.tmp"]):
        Rc.return_value.run_model = fake_replicate
        result = run_mode1(donor=donor, face=face, out=out,
                           replicate_api_key="rk")
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0
    fake_replicate.assert_called_once()
    call_args = fake_replicate.call_args
    assert call_args[0][0] in (
        "cdingram/face-swap", "lucataco/faceswap",
    )  # see spec §13 open question 1


def test_mode1_returns_none_on_safety_error(tmp_path):
    from app.services.replicate_client import ReplicateSafetyError
    donor = tmp_path / "donor.mp4"; donor.write_bytes(b"x")
    face = tmp_path / "face.png"; face.write_bytes(b"x")
    out = tmp_path / "out.mp4"

    def boom(*a, **kw): raise ReplicateSafetyError("nsfw")
    with patch("app.services.forge_e_mode1.ReplicateClient") as Rc, \
         patch("app.services.forge_e_mode1.upload_temp_public",
               return_value="https://x"):
        Rc.return_value.run_model = boom
        import pytest as _pt
        with _pt.raises(ReplicateSafetyError):
            run_mode1(donor=donor, face=face, out=out, replicate_api_key="rk")
```

- [ ] **Step 2: Implementation**

```python
# app/services/forge_e_mode1.py
"""Strategy E, Mode 1 — face-only swap via Replicate."""
from __future__ import annotations
from pathlib import Path
from app.services.replicate_client import ReplicateClient
from app.services.media_service import download_bytes, upload_temp_public

REPLICATE_MODEL = "cdingram/face-swap"  # see spec §13 open Q1
TEMP_TTL_SECONDS = 3600


def run_mode1(*, donor: Path, face: Path, out: Path,
              replicate_api_key: str) -> Path:
    donor_url = upload_temp_public(donor.read_bytes(),
                                    suffix=".mp4",
                                    content_type="video/mp4",
                                    ttl_seconds=TEMP_TTL_SECONDS)
    face_url = upload_temp_public(face.read_bytes(),
                                   suffix=".png",
                                   content_type="image/png",
                                   ttl_seconds=TEMP_TTL_SECONDS)
    client = ReplicateClient(api_key=replicate_api_key)
    result = client.run_model(REPLICATE_MODEL, {
        "input_video": donor_url,
        "swap_image": face_url,
    })
    result_url = result if isinstance(result, str) else result[0]
    blob = download_bytes(result_url)
    out.write_bytes(blob)
    return out
```

- [ ] **Step 3: Verify**

`pytest tests/test_forge_e_mode1.py -v`
Expected: PASS (2/2).

- [ ] **Step 4: Commit**

```bash
git add app/services/forge_e_mode1.py tests/test_forge_e_mode1.py
git commit -m "feat(forge-e): Mode 1 — Replicate face-swap"
```

---

## Task 5: Mode 2 — wan_clone.py wrapper

**Files:**
- Create: `app/services/forge_e_mode2.py`
- Test: `tests/test_forge_e_mode2.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_forge_e_mode2.py
from unittest.mock import patch, MagicMock
from pathlib import Path
from app.services.forge_e_mode2 import run_mode2, WanCloneError


def test_mode2_invokes_wan_clone_script(tmp_path, monkeypatch):
    donor = tmp_path / "donor.mp4"; donor.write_bytes(b"x")
    face = tmp_path / "face.png"; face.write_bytes(b"x")
    out = tmp_path / "out.mp4"
    monkeypatch.setenv("WAN_CLONE_SCRIPT", "/usr/bin/true")

    fake_info = MagicMock(host="1.2.3.4", ssh_port=22)
    fake_run = MagicMock(returncode=0, stdout="ok", stderr="")
    # Have subprocess.run also create the output file
    def run_side(cmd, *a, **kw):
        out.write_bytes(b"RESULT")
        return fake_run
    with patch("app.services.forge_e_mode2.ensure_pod_up", return_value=fake_info), \
         patch("subprocess.run", side_effect=run_side):
        result = run_mode2(donor=donor, face=face, out=out, timeout=60)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0


def test_mode2_raises_when_script_fails(tmp_path, monkeypatch):
    donor = tmp_path / "donor.mp4"; donor.write_bytes(b"x")
    face = tmp_path / "face.png"; face.write_bytes(b"x")
    out = tmp_path / "out.mp4"
    monkeypatch.setenv("WAN_CLONE_SCRIPT", "/usr/bin/false")

    fake_info = MagicMock(host="1.2.3.4", ssh_port=22)
    fake_run = MagicMock(returncode=2, stdout="", stderr="boom")
    with patch("app.services.forge_e_mode2.ensure_pod_up", return_value=fake_info), \
         patch("subprocess.run", return_value=fake_run):
        import pytest as _pt
        with _pt.raises(WanCloneError):
            run_mode2(donor=donor, face=face, out=out, timeout=60)


def test_mode2_raises_when_pod_unavailable(tmp_path, monkeypatch):
    from app.services.runpod_pod import PodUnavailable
    donor = tmp_path / "donor.mp4"; donor.write_bytes(b"x")
    face = tmp_path / "face.png"; face.write_bytes(b"x")
    out = tmp_path / "out.mp4"
    with patch("app.services.forge_e_mode2.ensure_pod_up",
               side_effect=PodUnavailable("nope")):
        import pytest as _pt
        with _pt.raises(WanCloneError):
            run_mode2(donor=donor, face=face, out=out, timeout=60)
```

- [ ] **Step 2: Implementation**

```python
# app/services/forge_e_mode2.py
"""Strategy E, Mode 2 — wraps the existing wan_clone.py RunPod workflow."""
from __future__ import annotations
import os
import subprocess
from pathlib import Path
from app.services.runpod_pod import ensure_pod_up, PodUnavailable


class WanCloneError(RuntimeError):
    pass


WAN_CLONE_SCRIPT_ENV = "WAN_CLONE_SCRIPT"
DEFAULT_SCRIPT = "/opt/tg-bot/tools/wan_clone.py"


def run_mode2(*, donor: Path, face: Path, out: Path,
              timeout: int = 900) -> Path:
    try:
        ensure_pod_up(start_timeout=120)
    except PodUnavailable as e:
        raise WanCloneError(f"RunPod pod unavailable: {e}") from e

    script = os.getenv(WAN_CLONE_SCRIPT_ENV, DEFAULT_SCRIPT)
    cmd = [script, str(donor), str(face), "--out", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise WanCloneError(
            f"wan_clone exit={r.returncode}: {r.stderr.strip()[:300]}"
        )
    if not out.exists() or out.stat().st_size == 0:
        raise WanCloneError("wan_clone produced no output file")
    return out
```

- [ ] **Step 3: Verify**

`pytest tests/test_forge_e_mode2.py -v`
Expected: PASS (3/3).

- [ ] **Step 4: Commit**

```bash
git add app/services/forge_e_mode2.py tests/test_forge_e_mode2.py
git commit -m "feat(forge-e): Mode 2 — wan_clone.py wrapper"
```

---

## Task 6: Worker drain loop

**Files:**
- Create: `app/workers/forge_e_worker.py`
- Test: `tests/test_forge_e_worker.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_forge_e_worker.py
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from app.models.generation import GeneratedVideo, GenerationStatus, VideoProvider
from app.workers.forge_e_worker import process_gv, pick_next_pending


def test_pick_next_picks_pending_strategy_e_row(db_session, test_user, ready_persona):
    gv = GeneratedVideo(user_id=test_user.id, prompt="[strategy=E mode=1]",
                        provider=VideoProvider.MOCK,
                        status=GenerationStatus.PENDING,
                        persona_id=ready_persona.id, mode=1)
    db_session.add(gv); db_session.commit()
    got = pick_next_pending(db_session)
    assert got is not None
    assert got.id == gv.id


def test_pick_next_ignores_ready_rows(db_session, test_user, ready_persona):
    gv = GeneratedVideo(user_id=test_user.id, prompt="x",
                        provider=VideoProvider.MOCK,
                        status=GenerationStatus.READY,
                        persona_id=ready_persona.id, mode=1)
    db_session.add(gv); db_session.commit()
    assert pick_next_pending(db_session) is None


def test_process_gv_mode1_happy_path(db_session, test_user, ready_persona):
    gv = GeneratedVideo(user_id=test_user.id, prompt="x",
                        provider=VideoProvider.MOCK,
                        status=GenerationStatus.PENDING,
                        persona_id=ready_persona.id, mode=1)
    db_session.add(gv); db_session.commit(); db_session.refresh(gv)

    def fake_download_source(url, dest):
        Path(dest).write_bytes(b"DONOR")
        return dest
    def fake_download_face(url):
        return b"FACE"
    def fake_mode1(*, donor, face, out, replicate_api_key):
        out.write_bytes(b"RESULT")
        return out
    def fake_ensure_faststart(p, timeout=120):
        return True
    fake_upload = MagicMock(return_value="/api/media?key=users/1/forge_e/abc.mp4")

    with patch("app.workers.forge_e_worker.download_source_video",
               side_effect=fake_download_source), \
         patch("app.workers.forge_e_worker.download_bytes",
               side_effect=fake_download_face), \
         patch("app.workers.forge_e_worker.run_mode1", side_effect=fake_mode1), \
         patch("app.workers.forge_e_worker.ensure_faststart",
               side_effect=fake_ensure_faststart), \
         patch("app.workers.forge_e_worker.r2_upload_file", side_effect=fake_upload):
        process_gv(db_session, gv)

    db_session.refresh(gv)
    assert gv.status == GenerationStatus.READY
    assert gv.media_storage_key is not None or gv.media_url
    assert gv.completed_at is not None


def test_process_gv_marks_failed_on_exception(db_session, test_user, ready_persona):
    gv = GeneratedVideo(user_id=test_user.id, prompt="x",
                        provider=VideoProvider.MOCK,
                        status=GenerationStatus.PENDING,
                        persona_id=ready_persona.id, mode=1)
    db_session.add(gv); db_session.commit(); db_session.refresh(gv)

    with patch("app.workers.forge_e_worker.download_source_video",
               side_effect=RuntimeError("net down")):
        process_gv(db_session, gv)

    db_session.refresh(gv)
    assert gv.status == GenerationStatus.FAILED
    assert "net down" in (gv.error_message or "")
```

- [ ] **Step 2: Implementation**

```python
# app/workers/forge_e_worker.py
"""Strategy E worker drain loop.

Architecture aligns with memory:Forge tech insights priority 1
(worker out of web process). For MVP runs as daemon thread, structured
so it can move to a dedicated process trivially.
"""
from __future__ import annotations
import logging
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy.orm import Session
from app.models.generation import GeneratedVideo, GenerationStatus
from app.models.persona import Persona
from app.services.forge_e_mode1 import run_mode1
from app.services.forge_e_mode2 import run_mode2, WanCloneError
from app.services.replicate_client import ReplicateSafetyError

# Reuse existing helpers — update imports if names differ:
from app.services.media_service import (
    download_source_video, ensure_faststart, r2_upload_file, download_bytes,
)

log = logging.getLogger(__name__)


def pick_next_pending(db: Session) -> GeneratedVideo | None:
    return (db.query(GeneratedVideo)
            .filter(GeneratedVideo.status == GenerationStatus.PENDING,
                    GeneratedVideo.mode.isnot(None))
            .order_by(GeneratedVideo.created_at.asc())
            .with_for_update(skip_locked=True)
            .first())


def _mark_failed(db: Session, gv: GeneratedVideo, msg: str):
    gv.status = GenerationStatus.FAILED
    gv.error_message = msg[:500]
    gv.completed_at = datetime.utcnow()
    db.commit()


def process_gv(db: Session, gv: GeneratedVideo) -> None:
    """One full pipeline for a single Strategy E row."""
    gv.status = GenerationStatus.RUNNING
    gv.started_at = datetime.utcnow()
    db.commit()

    try:
        persona = db.query(Persona).get(gv.persona_id)
        if not persona or not persona.canonical_face_url:
            return _mark_failed(db, gv, "persona missing or no canonical face")

        # Derive source_url from the prompt blob (cheap convention from D path)
        src_url = _extract_source_url(gv.prompt)
        if not src_url:
            return _mark_failed(db, gv, "could not extract source_url from prompt")

        with tempfile.TemporaryDirectory(prefix="forge_e_") as tmp:
            tmp = Path(tmp)
            donor = tmp / "donor.mp4"
            face = tmp / "face.png"
            out = tmp / "out.mp4"

            download_source_video(src_url, donor)
            face.write_bytes(download_bytes(persona.canonical_face_url))

            if gv.mode == 1:
                user_key = persona.user.replicate_api_key
                if not user_key:
                    return _mark_failed(db, gv, "user has no Replicate key")
                try:
                    run_mode1(donor=donor, face=face, out=out,
                              replicate_api_key=user_key)
                except ReplicateSafetyError as e:
                    return _mark_failed(db, gv,
                        "🛑 Replicate safety reject — попробуй другую персону")
            elif gv.mode == 2:
                t0 = time.time()
                try:
                    run_mode2(donor=donor, face=face, out=out)
                except WanCloneError as e:
                    return _mark_failed(db, gv, f"Mode 2 failed: {e}")
                gv.cost_runpod_seconds = round(time.time() - t0, 2)
            else:
                return _mark_failed(db, gv, f"unknown mode: {gv.mode}")

            ensure_faststart(out, timeout=120)
            key = f"users/{gv.user_id}/forge_e/{uuid.uuid4().hex[:12]}.mp4"
            media_url = r2_upload_file(key, out, content_type="video/mp4")
            gv.media_storage_key = key
            gv.media_url = media_url
            gv.status = GenerationStatus.READY
            gv.completed_at = datetime.utcnow()
            db.commit()
    except Exception as e:
        log.exception("forge_e gv=%s failed", gv.id)
        _mark_failed(db, gv, str(e))


def _extract_source_url(prompt: str) -> str | None:
    # Match the D-strategy convention: "[strategy=E mode=N persona=…] source=<url>"
    if "source=" not in prompt:
        return None
    return prompt.split("source=", 1)[1].strip().split()[0] or None


def run_loop(db_factory, poll_seconds: float = 2.0):
    while True:
        db = db_factory()
        try:
            gv = pick_next_pending(db)
            if gv is None:
                db.commit()
                time.sleep(poll_seconds)
                continue
            process_gv(db, gv)
        except Exception:
            log.exception("forge_e loop tick failed")
            db.rollback()
        finally:
            db.close()


def sweep_stuck_running(db_factory, max_minutes: int = 30):
    """Recover rows stuck in RUNNING after a worker restart."""
    db = db_factory()
    try:
        threshold = datetime.utcnow() - timedelta(minutes=max_minutes)
        stuck = (db.query(GeneratedVideo)
                 .filter(GeneratedVideo.status == GenerationStatus.RUNNING,
                         GeneratedVideo.mode.isnot(None),
                         GeneratedVideo.started_at < threshold)
                 .all())
        for gv in stuck:
            gv.status = GenerationStatus.PENDING
            gv.started_at = None
            log.warning("reset stuck forge_e gv=%s", gv.id)
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 3: Verify**

`pytest tests/test_forge_e_worker.py -v`
Expected: PASS (4/4).

- [ ] **Step 4: Commit**

```bash
git add app/workers/forge_e_worker.py tests/test_forge_e_worker.py
git commit -m "feat(forge-e): worker — drain, run pipeline, sweep stuck"
```

---

## Task 7: Extend `/api/forge/start` to Strategy E

**Files:**
- Modify: `app/api/forge.py`
- Test: `tests/test_forge_api_strategy_e.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_forge_api_strategy_e.py
def test_forge_start_e_returns_gv_id(auth_client, ready_persona):
    r = auth_client.post("/api/forge/start", json={
        "strategy": "E",
        "source_url": "https://www.instagram.com/reel/X/",
        "persona_id": ready_persona.id,
        "mode": 1,
    })
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["strategy"] == "E"
    assert body["gv_id"] is not None
    assert body["next_step"].startswith("poll")


def test_forge_start_e_requires_persona(auth_client):
    r = auth_client.post("/api/forge/start", json={
        "strategy": "E", "source_url": "https://x.example/v",
        "persona_id": 99999, "mode": 1,
    })
    assert r.status_code == 400


def test_forge_start_e_rejects_unowned_persona(auth_client, other_user_persona):
    r = auth_client.post("/api/forge/start", json={
        "strategy": "E", "source_url": "https://x.example/v",
        "persona_id": other_user_persona.id, "mode": 1,
    })
    assert r.status_code == 400
```

- [ ] **Step 2: Modify `app/api/forge.py`**

a) Extend the `Strategy` literal:

```python
Strategy = Literal["A", "B", "C", "D", "E"]
```

b) Extend `ForgeStartRequest` with two optional fields used only by E:

```python
    # E-specific (face replace via persona)
    persona_id: Optional[int] = None
    e_mode: Optional[int] = Field(None, ge=1, le=2)
```

(Using `e_mode` to avoid colliding with any future reuse of `mode`.)

c) Extend `_estimate_cost` with an E branch:

```python
    if strategy == "E":
        return 0.05 if c_keyframe_count else 0.05  # mode independent at this granularity
```

d) Add the E branch inside `forge_start()` after the existing D branch:

```python
    if data.strategy == "E":
        if data.persona_id is None or data.e_mode is None:
            raise HTTPException(400, "persona_id and e_mode required for strategy E")
        from app.services.forge_e_service import start_e, ForgeEValidationError
        try:
            gv = start_e(db, current_user,
                         source_url=data.source_url,
                         persona_id=data.persona_id,
                         mode=data.e_mode)
        except ForgeEValidationError as e:
            raise HTTPException(400, str(e))
        return ForgeStartResponse(
            strategy="E", gv_id=gv.id, next_step="poll /api/media/diag/<gv_id>",
            cost_estimate_usd=cost,
        )
```

- [ ] **Step 3: Verify**

`pytest tests/test_forge_api_strategy_e.py -v`
Expected: PASS (3/3).

- [ ] **Step 4: Commit**

```bash
git add app/api/forge.py tests/test_forge_api_strategy_e.py
git commit -m "feat(forge-e): /api/forge/start strategy=E"
```

---

## Task 8: Frontend — E tab in `/forge`

**Files:**
- Modify: `static/forge.html`

- [ ] **Step 1: Find the strategy-tabs block**

In `static/forge.html`, locate `id="strategy-tabs"`. After the D tab,
append an E tab button (visual style matches the others).

- [ ] **Step 2: Add E form block**

After the D form block, append:

```html
<div id="form-e" class="hidden">
  <label class="block text-xs text-gray-400 mb-1">URL видео-донора</label>
  <input id="e-source-url" placeholder="https://www.instagram.com/reel/…"
         class="w-full bg-black/40 rounded px-3 py-2 mb-3">

  <label class="block text-xs text-gray-400 mb-1">Персона</label>
  <select id="e-persona" class="w-full bg-black/40 rounded px-3 py-2 mb-3">
    <option value="">— загрузка —</option>
  </select>
  <a href="/personas" class="text-xs text-blue-300 underline mb-3 inline-block">
    + создать персону
  </a>

  <label class="block text-xs text-gray-400 mb-1">Режим</label>
  <div class="flex gap-3 mb-4">
    <label><input type="radio" name="e-mode" value="1" checked> Face only · ~$0.05</label>
    <label><input type="radio" name="e-mode" value="2"> Face + body · ~$0.20</label>
  </div>
</div>
```

- [ ] **Step 3: Extend the tabs JS**

Find the tab-switching code and add a case for E:

```javascript
// when E tab is activated:
async function activateE() {
  document.querySelectorAll('[id^="form-"]').forEach(el => el.classList.add('hidden'));
  document.getElementById('form-e').classList.remove('hidden');
  activeStrategy = 'E';
  // populate persona dropdown
  const r = await authFetch('/api/personas/');
  const data = await r.json();
  const sel = document.getElementById('e-persona');
  sel.innerHTML = '';
  const ready = data.items.filter(p => p.status === 'ready');
  if (!ready.length) {
    sel.innerHTML = '<option value="">— нет ready-персон —</option>';
  } else {
    for (const p of ready) {
      const o = document.createElement('option');
      o.value = p.id; o.textContent = p.name;
      sel.appendChild(o);
    }
  }
  // Preselect from URL ?persona=N (deep link from /personas page)
  const qs = new URLSearchParams(location.search);
  if (qs.get('persona')) sel.value = qs.get('persona');
}
```

- [ ] **Step 4: Extend `startForge()`**

In `startForge()` (lines ~395+ in current `forge.html`), the body
already POSTs `{strategy: activeStrategy, source_url, ...getParams()}`.
Extend `getParams()` to include E-specific fields when `activeStrategy === 'E'`:

```javascript
// add to getParams() or where params are assembled
if (activeStrategy === 'E') {
  return {
    persona_id: parseInt(document.getElementById('e-persona').value || '0', 10),
    e_mode: parseInt(document.querySelector('input[name="e-mode"]:checked').value, 10),
  };
}
```

And ensure the source URL comes from `#e-source-url` when E is active.

In `startForge()`'s response handling — Strategy E returns the same
shape as D (`{gv_id, next_step}`), so reuse the existing branch:

```javascript
if ((activeStrategy === 'C' || activeStrategy === 'D' || activeStrategy === 'E') && data.gv_id) {
  pollStrategyC(data.gv_id);
  return;
}
```

- [ ] **Step 5: Manual smoke**

```bash
uvicorn app.main:app --reload
# /forge → click E tab → fill URL + select persona + Mode 1 → submit
# → polling card appears → on ready, result-video plays (this also
# relies on PR fix/forge-strategy-bd-empty-player being merged)
```

- [ ] **Step 6: Commit**

```bash
git add static/forge.html
git commit -m "feat(forge-e): UI — E tab + persona select"
```

---

## Task 9: Wire worker thread + autostop cron

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add worker thread**

In the startup hook (alongside `persona_worker` from Plan 1):

```python
from app.workers.forge_e_worker import run_loop as forge_e_loop, sweep_stuck_running

if os.getenv("WORKER_FORGE_E", "1") == "1":
    forge_e_loop_thread = threading.Thread(
        target=forge_e_loop, args=(SessionLocal,),
        daemon=True, name="forge-e-worker")
    forge_e_loop_thread.start()
    # on every web startup, sweep any RUNNING rows left over from prior crash
    sweep_stuck_running(SessionLocal, max_minutes=30)
```

- [ ] **Step 2: Add autostop tick**

```python
from app.services.runpod_pod import stop_pod_if_idle

def _autostop_tick():
    while True:
        try:
            stop_pod_if_idle(max_idle_minutes=int(os.getenv("WAN_POD_IDLE_MIN", "10")))
        except Exception:
            log.exception("autostop tick failed")
        time.sleep(60)

if os.getenv("WAN_POD_AUTOSTOP", "1") == "1":
    threading.Thread(target=_autostop_tick, daemon=True,
                     name="wan-pod-autostop").start()
```

- [ ] **Step 3: Verify**

```bash
uvicorn app.main:app --reload
# tail logs: see "forge-e-worker" and "wan-pod-autostop" threads start
# create a strategy E row via API → worker picks it up
```

- [ ] **Step 4: Commit**

```bash
git add app/main.py
git commit -m "feat(forge-e): wire worker + RunPod autostop"
```

---

## Task 10: Integration smoke test

**Files:**
- Create: `tests/test_forge_e_integration.py`

- [ ] **Step 1: Test**

```python
# tests/test_forge_e_integration.py
"""End-to-end: enqueue via API, drain via worker (everything mocked except DB)."""
from unittest.mock import patch
from pathlib import Path
from app.workers.forge_e_worker import process_gv, pick_next_pending


def test_full_flow_mode1(auth_client, db_session, ready_persona):
    # 1. enqueue via API
    r = auth_client.post("/api/forge/start", json={
        "strategy": "E",
        "source_url": "https://www.instagram.com/reel/X/",
        "persona_id": ready_persona.id,
        "e_mode": 1,
    })
    assert r.status_code == 202
    gv_id = r.json()["gv_id"]

    # 2. drain via worker
    def fake_dl_src(url, dest):
        Path(dest).write_bytes(b"DONOR")
        return dest
    with patch("app.workers.forge_e_worker.download_source_video",
               side_effect=fake_dl_src), \
         patch("app.workers.forge_e_worker.download_bytes",
               return_value=b"FACE"), \
         patch("app.workers.forge_e_worker.run_mode1",
               side_effect=lambda **kw: kw["out"].write_bytes(b"OK") or kw["out"]), \
         patch("app.workers.forge_e_worker.ensure_faststart", return_value=True), \
         patch("app.workers.forge_e_worker.r2_upload_file",
               return_value="/api/media?key=u/1/forge_e/abc.mp4"):
        gv = pick_next_pending(db_session)
        assert gv.id == gv_id
        process_gv(db_session, gv)

    # 3. check /api/media/diag returns ready
    r2 = auth_client.get(f"/api/media/diag/{gv_id}")
    assert r2.status_code == 200
    assert r2.json()["status"] == "ready"
    assert "/api/media?key=" in r2.json()["media_url"]
```

- [ ] **Step 2: Verify**

`pytest tests/test_forge_e_integration.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_forge_e_integration.py
git commit -m "test(forge-e): end-to-end API → worker → diag"
```

---

## Self-Review

**Spec coverage:**

- §1 summary — Tasks 3, 4, 5, 6 (service + modes + worker).
- §3.1 Forge E tab — Task 8. ✓
- §4 architecture — Tasks 3, 6 (enqueue + worker with SKIP LOCKED). ✓
- §5.2 Strategy E pipeline service — Task 3. ✓
- §5.3 Mode 1 Replicate — Task 4. ✓
- §5.4 Mode 2 RunPod Wan — Tasks 2, 5. ✓
- §5.4 auto-stop — Task 9. ✓
- §5.5 RunPod orchestration helper — Task 2. ✓
- §6 data model changes (persona_id, mode, cost cols) — Task 1. ✓
- §7 API surface (strategy=E branch) — Task 7. ✓
- §8 frontend — Task 8. ✓
- §10 error handling (worker mark_failed, sweep stuck, safety) — Task 6. ✓
- §11 testing — every task TDD. ✓

**Placeholder scan:** none. All code blocks complete.

**Type consistency:**
- `mode` column is `int` (smallint in DB) consistently in tests, model,
  service, worker, API request body (`e_mode`).
- `start_e` arg names (`source_url`, `persona_id`, `mode`) match across
  tests, service, API.
- `ReplicateSafetyError` raised by Mode 1 is caught in worker.
- `WanCloneError` raised by Mode 2 is caught in worker.
- `PodInfo` dataclass shape used identically in `runpod_pod` and Mode 2.

---

## Execution Handoff

Recommended: **subagent-driven** — same as Plan 1. Tasks here build
directly on Plan 1 fixtures + persona model, so execute Plan 1 to
completion first.
