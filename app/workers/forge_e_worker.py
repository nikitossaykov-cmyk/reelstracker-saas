"""Strategy E worker drain loop.

Aligns with memory:Forge tech insights priority 1 (worker out of the
web process). For MVP this still runs as a daemon thread alongside
uvicorn — same as the existing parser_worker / scheduler — but the
structure is drawn so a move to a dedicated Railway service is just
a deploy config change, not a code change.

The pipeline per row:
  1. mark RUNNING, set started_at
  2. resolve persona → canonical_face_url
  3. download donor video (reuses the D-strategy download path)
  4. branch on mode (1 → Replicate face-swap, 2 → wan_clone.py)
  5. ensure_faststart on the result
  6. upload to R2 under users/{uid}/forge_e/{hex}.mp4
  7. mark READY, set media_url + completed_at

A separate sweep_stuck_running pass is called at worker startup so
rows left in RUNNING by a previous crashed worker are returned to
PENDING instead of being stranded forever.
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
from app.services.media_helpers import download_bytes
from app.core.faststart import ensure_faststart
from app.core.yt_downloader import download_video
from app.core.storage import get_r2


log = logging.getLogger(__name__)


def pick_next_pending(db: Session) -> GeneratedVideo | None:
    return (
        db.query(GeneratedVideo)
        .filter(
            GeneratedVideo.status == GenerationStatus.PENDING,
            GeneratedVideo.mode.isnot(None),
        )
        .order_by(GeneratedVideo.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )


def _mark_failed(db: Session, gv: GeneratedVideo, msg: str) -> None:
    gv.status = GenerationStatus.FAILED
    gv.error_message = msg[:500]
    gv.completed_at = datetime.utcnow()
    db.commit()


def _extract_source_url(prompt: str) -> str | None:
    if "source=" not in prompt:
        return None
    tail = prompt.split("source=", 1)[1].strip().split()
    return tail[0] if tail else None


def process_gv(db: Session, gv: GeneratedVideo) -> None:
    """One full pipeline for a single Strategy E row."""
    gv.status = GenerationStatus.RUNNING
    gv.started_at = datetime.utcnow()
    db.commit()

    try:
        persona = db.query(Persona).get(gv.persona_id)
        if not persona or not persona.canonical_face_url:
            return _mark_failed(db, gv, "persona missing or no canonical face")

        src_url = _extract_source_url(gv.prompt or "")
        if not src_url:
            return _mark_failed(
                db, gv, "could not extract source_url from prompt"
            )

        with tempfile.TemporaryDirectory(prefix="forge_e_") as tmp_str:
            tmp = Path(tmp_str)
            face = tmp / "face.png"
            out = tmp / "out.mp4"

            apify_token = getattr(persona.user, "apify_token", None)
            downloaded, _meta = download_video(
                src_url, out_dir=tmp, apify_token=apify_token,
            )
            donor = downloaded
            face.write_bytes(download_bytes(persona.canonical_face_url))

            if gv.mode == 1:
                user_key = persona.user.replicate_api_key
                if not user_key:
                    return _mark_failed(db, gv, "user has no Replicate key")
                try:
                    run_mode1(
                        donor=donor,
                        face=face,
                        out=out,
                        replicate_api_key=user_key,
                    )
                except ReplicateSafetyError:
                    return _mark_failed(
                        db,
                        gv,
                        "🛑 Replicate safety reject — попробуй другую персону",
                    )
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
            r2 = get_r2()
            r2.upload_bytes(key, out.read_bytes(), content_type="video/mp4")
            gv.media_storage_key = key
            gv.media_url = r2.get_proxy_url(key)
            gv.status = GenerationStatus.READY
            gv.completed_at = datetime.utcnow()
            db.commit()
    except Exception as e:
        log.exception("forge_e gv=%s failed", gv.id)
        _mark_failed(db, gv, str(e))


def run_loop(db_factory, poll_seconds: float = 2.0) -> None:
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


def sweep_stuck_running(db_factory, max_minutes: int = 30) -> int:
    """Recover rows stuck in RUNNING after a worker restart.

    Returns the number of rows reset. Call from app startup before
    starting the run_loop thread.
    """
    db = db_factory()
    try:
        threshold = datetime.utcnow() - timedelta(minutes=max_minutes)
        stuck = (
            db.query(GeneratedVideo)
            .filter(
                GeneratedVideo.status == GenerationStatus.RUNNING,
                GeneratedVideo.mode.isnot(None),
                GeneratedVideo.started_at < threshold,
            )
            .all()
        )
        for gv in stuck:
            gv.status = GenerationStatus.PENDING
            gv.started_at = None
            log.warning("reset stuck forge_e gv=%s", gv.id)
        db.commit()
        return len(stuck)
    finally:
        db.close()
