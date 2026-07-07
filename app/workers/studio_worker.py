"""Drains StudioJob rows: PENDING → PORTRAIT → VOICEOVER → LIPSYNC →
ASSEMBLE → JUDGE → READY / FAILED. Same polling pattern as
makeugc_worker; one job at a time; stage keys + cost_usd written as it
goes so a retry after FAILED skips completed stages."""
from __future__ import annotations

import logging
import os
import tempfile
import time
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.storage import get_r2
from app.models.studio_job import StudioJob, StudioStatus
from app.models.user import User
from app.services.media_helpers import download_bytes
from app.services.replicate_client import (
    ReplicateError,
    ReplicateSafetyError,
    ReplicateTransientError,
)
from app.services.strategy_makeugc.lipsync import generate_lipsync
from app.services.strategy_makeugc.voiceover import (
    VoiceoverError,
    resolve_api_key as resolve_eleven_key,
    resolve_voice_id,
)
from app.services.strategy_single_take.assemble import (
    AssembleError,
    burn_captions,
    concat_clips,
    detect_silences,
    normalize_clip,
    polish,
    probe_duration,
)
from app.services.strategy_single_take.captions import (
    SILENCE_ASMR,
    SILENCE_NORMAL,
    align_sentences,
    build_ass,
    parse_silencedetect,
    speech_spans,
    split_sentences,
)
from app.services.strategy_single_take.judge import judge_video
from app.services.strategy_single_take.portrait import generate_studio_portrait
from app.services.strategy_single_take.voiceover import generate_voiceover_v3


ELEVENLABS_USD_PER_1K_CHARS = 0.30

log = logging.getLogger(__name__)


def pick_next_pending(db: Session) -> StudioJob | None:
    return (
        db.query(StudioJob)
        .filter(StudioJob.status == StudioStatus.PENDING)
        .order_by(StudioJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )


def _fail(db: Session, j: StudioJob, msg: str) -> None:
    j.status = StudioStatus.FAILED
    j.error_message = msg[:500]
    j.completed_at = datetime.utcnow()
    db.commit()


def _mark(db: Session, j: StudioJob, stage: StudioStatus) -> None:
    j.status = stage
    db.commit()


def _add_cost(db: Session, j: StudioJob, usd: float) -> None:
    j.cost_usd = (j.cost_usd or Decimal("0")) + Decimal(str(round(usd, 4)))
    db.commit()


def _get_blob(r2, key: str) -> bytes:
    obj = r2._client.get_object(Bucket=r2.bucket, Key=key)
    return obj["Body"].read()


def _to_bytes(result, timeout: int) -> bytes:
    if isinstance(result, (bytes, bytearray)):
        return bytes(result)
    return download_bytes(result, timeout=timeout)


def _assemble(
    j: StudioJob,
    tmp: Path,
    lipsync_path: Path,
    voiceover_path: Path,
    hook_path: Path | None,
) -> Path:
    """normalize → optional hook concat → captions → polish. Returns final mp4."""
    body = normalize_clip(lipsync_path, tmp / "body.mp4")
    hook_seconds = 0.0
    if hook_path is not None:
        hook_n = normalize_clip(hook_path, tmp / "hook_n.mp4")
        hook_seconds = probe_duration(hook_n)
        raw = concat_clips([hook_n, body], tmp / "raw.mp4")
    else:
        raw = body

    staged = raw
    if j.captions_enabled and j.script_text:
        noise, min_d = SILENCE_ASMR if j.voice_style == "asmr" else SILENCE_NORMAL
        stderr = detect_silences(voiceover_path, noise=noise, min_d=min_d)
        vo_total = probe_duration(voiceover_path)
        spans = speech_spans(parse_silencedetect(stderr), total=vo_total)
        sents = split_sentences(j.script_text)
        aligned = align_sentences(sents, spans)
        # shift into final timeline (hook precedes the talking take)
        aligned = [(s + hook_seconds, e + hook_seconds, t) for s, e, t in aligned]
        ass_path = tmp / "captions.ass"
        ass_path.write_text(build_ass(aligned))
        staged = burn_captions(raw, ass_path, tmp / "subbed.mp4")

    return polish(staged, tmp / "final.mp4", hook_seconds=hook_seconds)


def process_job(db: Session, j: StudioJob, user: User) -> None:
    replicate_key = user.replicate_api_key or os.getenv("REPLICATE_API_TOKEN")
    if not replicate_key:
        _fail(db, j, "Нет Replicate ключа (user или env REPLICATE_API_TOKEN)")
        return

    r2 = get_r2()
    keys = list(j.product_image_keys or [])
    if not keys:
        _fail(db, j, "У job'а нет product image — удали и пересоздай")
        return
    product_bytes = _get_blob(r2, keys[0])

    # --- PORTRAIT (resume-safe) ---
    if not j.portrait_key:
        _mark(db, j, StudioStatus.PORTRAIT)
        try:
            result, cost = generate_studio_portrait(
                product_image_bytes=product_bytes,
                product_content_type="image/jpeg",
                product_name=j.product_name,
                brand=j.brand,
                asmr=(j.voice_style == "asmr"),
                replicate_api_key=replicate_key,
            )
            blob = _to_bytes(result, timeout=120)
        except ReplicateSafetyError as e:
            _fail(db, j, f"🛑 Moderation отбила portrait: {str(e)[:200]}")
            return
        except ReplicateTransientError as e:
            log.warning("studio %s transient on portrait: %s", j.id, e)
            raise
        except Exception as e:
            log.exception("studio %s portrait fail", j.id)
            _fail(db, j, f"ошибка portrait: {str(e)[:200]}")
            return
        key = f"users/{j.user_id}/studio/{j.id}/portrait-{uuid.uuid4().hex[:6]}.jpg"
        r2.upload_bytes(key, blob, content_type="image/jpeg")
        j.portrait_key = key
        _add_cost(db, j, cost)

    # --- VOICEOVER ---
    if not j.voiceover_key:
        _mark(db, j, StudioStatus.VOICEOVER)
        if not j.script_text:
            _fail(db, j, "Нет script_text — сгенерируй или введи текст")
            return
        eleven_key = resolve_eleven_key(user.elevenlabs_api_key)
        voice_id = resolve_voice_id(None)
        if not eleven_key or not voice_id:
            _fail(db, j, "Нет ELEVENLABS_API_KEY или MAKEUGC_DEFAULT_VOICE_ID")
            return
        try:
            audio = generate_voiceover_v3(
                script_text=j.script_text,
                voice_id=voice_id,
                api_key=eleven_key,
                asmr=(j.voice_style == "asmr"),
            )
        except VoiceoverError as e:
            _fail(db, j, f"TTS ошибка: {str(e)[:200]}")
            return
        key = f"users/{j.user_id}/studio/{j.id}/voiceover-{uuid.uuid4().hex[:6]}.mp3"
        r2.upload_bytes(key, audio, content_type="audio/mpeg")
        j.voiceover_key = key
        _add_cost(db, j, len(j.script_text) * ELEVENLABS_USD_PER_1K_CHARS / 1000.0)

    # --- LIPSYNC ---
    if not j.lipsync_key:
        _mark(db, j, StudioStatus.LIPSYNC)
        try:
            lip_result, lip_cost = generate_lipsync(
                portrait_bytes=_get_blob(r2, j.portrait_key),
                portrait_ext="jpg",
                voiceover_bytes=_get_blob(r2, j.voiceover_key),
                voiceover_ext="mp3",
                replicate_api_key=replicate_key,
            )
            video_blob = _to_bytes(lip_result, timeout=300)
        except ReplicateSafetyError as e:
            _fail(db, j, f"🛑 Moderation отбила lipsync: {str(e)[:200]}")
            return
        except ReplicateTransientError as e:
            log.warning("studio %s transient on lipsync: %s", j.id, e)
            raise
        except ReplicateError as e:
            _fail(db, j, f"Replicate fail on lipsync: {str(e)[:200]}")
            return
        except Exception as e:
            log.exception("studio %s lipsync fail", j.id)
            _fail(db, j, f"ошибка lipsync: {str(e)[:200]}")
            return
        key = f"users/{j.user_id}/studio/{j.id}/lipsync-{uuid.uuid4().hex[:6]}.mp4"
        r2.upload_bytes(key, video_blob, content_type="video/mp4")
        j.lipsync_key = key
        _add_cost(db, j, lip_cost)

    # --- ASSEMBLE + JUDGE (same tmpdir) ---
    _mark(db, j, StudioStatus.ASSEMBLE)
    with tempfile.TemporaryDirectory(prefix="studio_") as tmpdir:
        tmp = Path(tmpdir)
        lipsync_path = tmp / "lipsync.mp4"
        lipsync_path.write_bytes(_get_blob(r2, j.lipsync_key))
        voiceover_path = tmp / "voiceover.mp3"
        voiceover_path.write_bytes(_get_blob(r2, j.voiceover_key))
        hook_path: Path | None = None
        if j.hook_video_key:
            hook_path = tmp / "hook_src.mp4"
            hook_path.write_bytes(_get_blob(r2, j.hook_video_key))

        try:
            final_path = _assemble(j, tmp, lipsync_path, voiceover_path, hook_path)
        except AssembleError as e:
            _fail(db, j, f"ffmpeg pipeline failed: {str(e)[:300]}")
            return
        except Exception as e:
            log.exception("studio %s assemble fail", j.id)
            _fail(db, j, f"ошибка assemble: {str(e)[:200]}")
            return

        out_key = f"users/{j.user_id}/studio/{j.id}/final-{uuid.uuid4().hex[:6]}.mp4"
        r2.upload_bytes(out_key, final_path.read_bytes(), content_type="video/mp4")
        j.output_key = out_key
        db.commit()

        # --- JUDGE (non-blocking) ---
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key:
            _mark(db, j, StudioStatus.JUDGE)
            try:
                report = judge_video(
                    final_path, api_key=gemini_key,
                    brief=(
                        f"AI-UGC single-take рилс: аналог {j.brand} "
                        f"{j.product_name}, стиль {j.voice_style}"
                    ),
                )
                j.judge_report = report
                overall = report.get("overall")
                if isinstance(overall, (int, float)):
                    j.judge_score = int(round(overall))
                db.commit()
            except Exception as e:  # non-blocking by design
                log.warning("studio %s judge failed (non-blocking): %s", j.id, e)
                db.rollback()

    j.status = StudioStatus.READY
    j.completed_at = datetime.utcnow()
    db.commit()


def run_loop(db_factory, poll_seconds: float = 2.0) -> None:
    """Long-running drain loop. db_factory() returns a fresh Session per tick."""
    while True:
        db = db_factory()
        try:
            j = pick_next_pending(db)
            if j is None:
                db.commit()
                time.sleep(poll_seconds)
                continue
            try:
                user = db.query(User).get(j.user_id)
                if not user:
                    _fail(db, j, "user disappeared")
                    continue
                process_job(db, j, user)
            except ReplicateTransientError:
                db.rollback()
            except Exception as e:
                log.exception("studio %s hard fail", j.id)
                _fail(db, j, f"внутренняя ошибка: {str(e)[:200]}")
        finally:
            db.close()
