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
    CUTAWAY_SECONDS,
    burn_captions,
    concat_clips,
    cut_clip,
    detect_silences,
    normalize_clip,
    polish,
    probe_duration,
    still_to_clip,
)
from app.services.strategy_single_take.captions import (
    SILENCE_ASMR,
    SILENCE_NORMAL,
    align_sentences,
    build_ass,
    parse_silencedetect,
    pick_insert_gap,
    shift_captions,
    speech_spans,
    split_sentences,
)
from app.services.strategy_single_take.cutaways import (
    KINDS as CUTAWAY_KINDS,
    animate_cutaway,
    generate_cutaway_still,
)
from app.services.strategy_single_take.judge import judge_video
from app.services.strategy_single_take.portrait import (
    generate_persona_portrait,
    generate_studio_portrait,
)
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
    insert_paths: list[Path] | None = None,
) -> Path:
    """normalize → optional cutaway splice → optional hook concat →
    captions → polish. Returns final mp4."""
    body = normalize_clip(lipsync_path, tmp / "body.mp4")

    # VO analysis (needed for both splice point and captions)
    noise, min_d = SILENCE_ASMR if j.voice_style == "asmr" else SILENCE_NORMAL
    stderr = detect_silences(voiceover_path, noise=noise, min_d=min_d)
    vo_total = probe_duration(voiceover_path)
    spans = speech_spans(parse_silencedetect(stderr), total=vo_total)

    # --- cutaway splice (optional) ---
    split_at: float | None = None
    inserts_seconds = 0.0
    parts: list[Path] = [body]
    if insert_paths:
        split_at = pick_insert_gap(spans, vo_total)
        log.info("studio %s cutaway split_at=%s (spans=%d)", j.id, split_at, len(spans))
    if split_at is not None:
        body_a = cut_clip(body, tmp / "body_a.mp4", start=0.0, end=split_at)
        body_b = cut_clip(body, tmp / "body_b.mp4", start=split_at)
        trimmed: list[Path] = []
        for i, ins in enumerate(insert_paths):
            ins_n = normalize_clip(ins, tmp / f"ins_n_{i}.mp4")
            trimmed.append(
                cut_clip(ins_n, tmp / f"ins_{i}.mp4",
                         start=0.0, end=CUTAWAY_SECONDS)
            )
        inserts_seconds = CUTAWAY_SECONDS * len(trimmed)
        parts = [body_a, *trimmed, body_b]

    hook_seconds = 0.0
    if hook_path is not None:
        hook_n = normalize_clip(hook_path, tmp / "hook_n.mp4")
        hook_seconds = probe_duration(hook_n)
        parts = [hook_n, *parts]

    raw = concat_clips(parts, tmp / "raw.mp4") if len(parts) > 1 else parts[0]

    staged = raw
    if j.captions_enabled and j.script_text:
        sents = split_sentences(j.script_text)
        aligned = align_sentences(sents, spans)
        # shift for inserts FIRST (split_at is in the VO timeline),
        # THEN into the final timeline (hook precedes the take)
        if split_at is not None and inserts_seconds > 0:
            aligned = shift_captions(aligned, split_at, inserts_seconds)
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
        persona_key = user.studio_persona_key if j.use_persona else None
        try:
            if persona_key:
                result, cost = generate_persona_portrait(
                    persona_bytes=_get_blob(r2, persona_key),
                    product_image_bytes=product_bytes,
                    product_content_type="image/jpeg",
                    product_name=j.product_name,
                    brand=j.brand,
                    asmr=(j.voice_style == "asmr"),
                    look_prompt=j.look_prompt,
                    replicate_api_key=replicate_key,
                )
            else:
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
        if persona_key and j.look_prompt:
            # новый образ становится каноном; без look_prompt канон не
            # трогаем — иначе копия-с-копии дрифтует от поколения к поколению
            user.studio_persona_key = key
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

    # --- CUTAWAYS (optional, non-blocking — a reel without inserts still ships) ---
    if j.cutaways_enabled:
        _mark(db, j, StudioStatus.CUTAWAYS)
        for kind in CUTAWAY_KINDS:  # ("cap_off", "spray")
            clip_attr = "cap_clip_key" if kind == "cap_off" else "spray_clip_key"
            still_attr = "cap_still_key" if kind == "cap_off" else "spray_still_key"
            if getattr(j, clip_attr):
                continue  # resume-safe
            try:
                if not getattr(j, still_attr):
                    result, cost = generate_cutaway_still(
                        portrait_bytes=_get_blob(r2, j.portrait_key),
                        product_bytes=product_bytes,
                        kind=kind,
                        product_name=j.product_name,
                        brand=j.brand,
                        replicate_api_key=replicate_key,
                    )
                    blob = _to_bytes(result, timeout=120)
                    key = (f"users/{j.user_id}/studio/{j.id}/"
                           f"cutaway-{kind}-{uuid.uuid4().hex[:6]}.jpg")
                    r2.upload_bytes(key, blob, content_type="image/jpeg")
                    setattr(j, still_attr, key)
                    _add_cost(db, j, cost)
                result, cost = animate_cutaway(
                    still_bytes=_get_blob(r2, getattr(j, still_attr)),
                    kind=kind,
                    replicate_api_key=replicate_key,
                )
                blob = _to_bytes(result, timeout=300)
                key = (f"users/{j.user_id}/studio/{j.id}/"
                       f"cutaway-{kind}-{uuid.uuid4().hex[:6]}.mp4")
                r2.upload_bytes(key, blob, content_type="video/mp4")
                setattr(j, clip_attr, key)
                _add_cost(db, j, cost)
            except ReplicateTransientError as e:
                log.warning("studio %s transient on cutaway %s: %s", j.id, kind, e)
                raise
            except Exception as e:
                # non-blocking: still (if any) stays for static fallback
                log.warning("studio %s cutaway %s failed (non-blocking): %s",
                            j.id, kind, e)
                db.rollback()

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

        insert_paths: list[Path] = []
        if j.cutaways_enabled:
            for kind, clip_attr, still_attr in (
                ("cap_off", "cap_clip_key", "cap_still_key"),
                ("spray", "spray_clip_key", "spray_still_key"),
            ):
                clip_key = getattr(j, clip_attr)
                still_key = getattr(j, still_attr)
                try:
                    if clip_key:
                        p = tmp / f"cut_{kind}.mp4"
                        p.write_bytes(_get_blob(r2, clip_key))
                        insert_paths.append(p)
                    elif still_key:
                        jpg = tmp / f"cut_{kind}.jpg"
                        jpg.write_bytes(_get_blob(r2, still_key))
                        insert_paths.append(
                            still_to_clip(jpg, tmp / f"cut_{kind}_static.mp4")
                        )
                except Exception as e:
                    log.warning("studio %s insert %s unusable (skip): %s",
                                j.id, kind, e)

        try:
            final_path = _assemble(
                j, tmp, lipsync_path, voiceover_path, hook_path,
                insert_paths=insert_paths or None,
            )
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
