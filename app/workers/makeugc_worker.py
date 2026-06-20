"""Drains MakeUGCJob rows through the pipeline stages.

Current scope: PENDING → PORTRAIT → VOICEOVER → READY. READY here means
"talking-head materials assembled" — the lipsync / cutaway / concat
stages that turn them into a finished mp4 land in subsequent PRs as
their modules arrive.

Row locking via SELECT ... FOR UPDATE SKIP LOCKED so the loop can be
moved to a separate process later without losing exactly-once
semantics.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.storage import get_r2
from app.models.makeugc_job import MakeUGCJob, MakeUGCStatus
from app.models.user import User
from app.services.media_helpers import download_bytes
from app.services.replicate_client import (
    ReplicateError,
    ReplicateSafetyError,
    ReplicateTransientError,
)
from app.services.strategy_makeugc.portrait import (
    MODEL_MAX,
    generate_portrait,
)
from app.services.strategy_makeugc.bottle_hero import generate_bottle_hero
from app.services.strategy_makeugc.concat import ConcatError, concat_parts
from app.services.strategy_makeugc.cutaway import (
    CutawayError,
    apply_image_cutaway,
)
from app.services.strategy_makeugc.lipsync import generate_lipsync
from app.services.strategy_makeugc.script import (
    generate_script,
    resolve_openai_key,
)
from app.services.strategy_makeugc.voiceover import (
    VoiceoverError,
    generate_voiceover,
    resolve_api_key as resolve_eleven_key,
    resolve_voice_id,
)
from app.services.strategy_makeugc import quota as makeugc_quota


# ElevenLabs eleven_multilingual_v2: $0.30 per 1000 chars on the helper-
# bot's current tier. Captured per-stage so partial reels still bill.
ELEVENLABS_USD_PER_1K_CHARS = 0.30


log = logging.getLogger(__name__)


def pick_next_pending(db: Session) -> MakeUGCJob | None:
    return (
        db.query(MakeUGCJob)
        .filter(MakeUGCJob.status == MakeUGCStatus.PENDING)
        .order_by(MakeUGCJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )


def _fail(db: Session, j: MakeUGCJob, msg: str) -> None:
    j.status = MakeUGCStatus.FAILED
    j.error_message = msg[:500]
    j.completed_at = datetime.utcnow()
    db.commit()


def _mark_stage(db: Session, j: MakeUGCJob, stage: MakeUGCStatus) -> None:
    j.status = stage
    db.commit()


def process_job(db: Session, j: MakeUGCJob, user: User) -> None:
    replicate_api_key = user.replicate_api_key
    if not replicate_api_key:
        _fail(db, j, "У пользователя нет Replicate API key — задай его в /settings")
        return

    r2 = get_r2()
    # Fetch product image once — needed by portrait stage and possibly
    # bottle-hero stage downstream.
    product_obj = r2._client.get_object(
        Bucket=r2.bucket, Key=j.product_image_key
    )
    product_bytes = product_obj["Body"].read()
    product_ct = product_obj.get("ContentType") or "image/jpeg"

    # --- Stage: PORTRAIT (skip if artifact already exists — resume safe) ---
    if not j.portrait_key:
        _mark_stage(db, j, MakeUGCStatus.PORTRAIT)
        try:
            result, cost = generate_portrait(
                product_image_bytes=product_bytes,
                product_content_type=product_ct,
                persona_style=j.persona_style,
                replicate_api_key=replicate_api_key,
                model=MODEL_MAX,
            )
            if isinstance(result, (bytes, bytearray)):
                blob = bytes(result)
            else:
                blob = download_bytes(result, timeout=120)
            key = (
                f"users/{j.user_id}/makeugc/{j.id}/"
                f"portrait-{uuid.uuid4().hex[:6]}.jpg"
            )
            r2.upload_bytes(key, blob, content_type="image/jpeg")
            j.portrait_key = key
            j.cost_usd = (j.cost_usd or Decimal("0")) + Decimal(str(cost))
            db.commit()
        except ReplicateSafetyError as e:
            _fail(db, j, f"🛑 Moderation отбила portrait prompt: {str(e)[:200]}")
            return
        except ReplicateTransientError as e:
            log.warning("makeugc %s transient on portrait: %s", j.id, e)
            raise
        except ReplicateError as e:
            _fail(db, j, f"Replicate hard fail on portrait: {str(e)[:200]}")
            return
        except Exception as e:
            log.exception("makeugc %s unexpected fail on portrait", j.id)
            _fail(db, j, f"внутренняя ошибка portrait: {str(e)[:200]}")
            return

    # --- Stage: VOICEOVER (skip if artifact already exists) ---
    if not j.voiceover_key:
        _mark_stage(db, j, MakeUGCStatus.VOICEOVER)

        openai_key = resolve_openai_key(user.openai_api_key)
        if not openai_key:
            _fail(db, j, "Нет OPENAI_API_KEY (ни у юзера, ни в env) — нужен для script gen")
            return
        eleven_key = resolve_eleven_key(user.elevenlabs_api_key)
        voice_id = resolve_voice_id(None)
        if not eleven_key or not voice_id:
            _fail(db, j, "Нет ELEVENLABS_API_KEY или MAKEUGC_DEFAULT_VOICE_ID в env")
            return

        if not j.script_text:
            try:
                j.script_text = generate_script(
                    product_name=j.product_name,
                    premium_brand=j.premium_brand,
                    premium_price_usd=float(j.premium_price_usd),
                    mimic_price_usd=float(j.mimic_price_usd),
                    persona_style=j.persona_style,
                    openai_api_key=openai_key,
                )
                db.commit()
            except Exception as e:
                log.exception("makeugc %s script-gen failed", j.id)
                _fail(db, j, f"script-gen ошибка: {str(e)[:200]}")
                return

        script_text = j.script_text

        using_shared_key = not user.elevenlabs_api_key
        if using_shared_key:
            makeugc_quota.maybe_reset_counter(db, user)
            ok, remaining = makeugc_quota.has_budget(
                user, chars_needed=len(script_text)
            )
            if not ok:
                _fail(
                    db, j,
                    f"Лимит общего пула ElevenLabs исчерпан "
                    f"(осталось {remaining} символов до начала следующего "
                    f"месяца). Подключи свой ключ в /settings или подожди."
                )
                return

        try:
            audio_bytes = generate_voiceover(
                script_text=script_text,
                voice_id=voice_id,
                api_key=eleven_key,
            )
        except VoiceoverError as e:
            _fail(db, j, f"TTS ошибка: {str(e)[:200]}")
            return
        except Exception as e:
            log.exception("makeugc %s tts unexpected fail", j.id)
            _fail(db, j, f"внутренняя ошибка tts: {str(e)[:200]}")
            return

        audio_key = (
            f"users/{j.user_id}/makeugc/{j.id}/"
            f"voiceover-{uuid.uuid4().hex[:6]}.mp3"
        )
        r2.upload_bytes(audio_key, audio_bytes, content_type="audio/mpeg")
        j.voiceover_key = audio_key

        tts_cost = round(
            len(script_text) * ELEVENLABS_USD_PER_1K_CHARS / 1000.0, 4
        )
        j.cost_usd = (j.cost_usd or Decimal("0")) + Decimal(str(tts_cost))

        if using_shared_key:
            makeugc_quota.consume(db, user, chars=len(script_text))
        db.commit()

    # --- Stage: LIPSYNC (skip if artifact already exists) ---
    if not j.lipsync_key:
        _mark_stage(db, j, MakeUGCStatus.LIPSYNC)

        try:
            portrait_obj = r2._client.get_object(Bucket=r2.bucket, Key=j.portrait_key)
            portrait_blob = portrait_obj["Body"].read()
            voice_obj = r2._client.get_object(Bucket=r2.bucket, Key=j.voiceover_key)
            voice_blob = voice_obj["Body"].read()

            lip_result, lip_cost = generate_lipsync(
                portrait_bytes=portrait_blob,
                portrait_ext="jpg",
                voiceover_bytes=voice_blob,
                voiceover_ext="mp3",
                replicate_api_key=replicate_api_key,
            )
            if isinstance(lip_result, (bytes, bytearray)):
                video_blob = bytes(lip_result)
            else:
                video_blob = download_bytes(lip_result, timeout=300)
        except ReplicateSafetyError as e:
            _fail(db, j, f"🛑 Moderation отбила lipsync: {str(e)[:200]}")
            return
        except ReplicateTransientError as e:
            log.warning("makeugc %s transient on lipsync: %s", j.id, e)
            raise
        except ReplicateError as e:
            _fail(db, j, f"Replicate hard fail on lipsync: {str(e)[:200]}")
            return
        except Exception as e:
            log.exception("makeugc %s lipsync unexpected fail", j.id)
            _fail(db, j, f"внутренняя ошибка lipsync: {str(e)[:200]}")
            return

        lip_key = (
            f"users/{j.user_id}/makeugc/{j.id}/"
            f"lipsync-{uuid.uuid4().hex[:6]}.mp4"
        )
        r2.upload_bytes(lip_key, video_blob, content_type="video/mp4")
        j.lipsync_key = lip_key
        j.cost_usd = (j.cost_usd or Decimal("0")) + Decimal(str(lip_cost))
        db.commit()

    # --- Stage: CUTAWAY (bottle-hero auto-gen + ffmpeg insert) ---
    if j.broll_video_key:
        _fail(
            db, j,
            "B-roll video upload путь ещё не реализован — пока auto-gen "
            "bottle-hero. Запусти job без broll_video_key."
        )
        return

    if not j.bottle_hero_key:
        _mark_stage(db, j, MakeUGCStatus.CUTAWAY)
        try:
            bottle_result, bottle_cost = generate_bottle_hero(
                product_image_bytes=product_bytes,
                product_content_type=product_ct,
                replicate_api_key=replicate_api_key,
            )
            if isinstance(bottle_result, (bytes, bytearray)):
                bottle_blob = bytes(bottle_result)
            else:
                bottle_blob = download_bytes(bottle_result, timeout=120)
        except ReplicateError as e:
            _fail(db, j, f"Replicate fail on bottle-hero: {str(e)[:200]}")
            return
        except Exception as e:
            log.exception("makeugc %s bottle-hero fail", j.id)
            _fail(db, j, f"bottle-hero ошибка: {str(e)[:200]}")
            return

        bottle_key = (
            f"users/{j.user_id}/makeugc/{j.id}/"
            f"bottle-hero-{uuid.uuid4().hex[:6]}.jpg"
        )
        r2.upload_bytes(bottle_key, bottle_blob, content_type="image/jpeg")
        j.bottle_hero_key = bottle_key
        j.cost_usd = (j.cost_usd or Decimal("0")) + Decimal(str(bottle_cost))
        db.commit()
    else:
        _mark_stage(db, j, MakeUGCStatus.CUTAWAY)
        bottle_obj = r2._client.get_object(
            Bucket=r2.bucket, Key=j.bottle_hero_key
        )
        bottle_blob = bottle_obj["Body"].read()

    # Always need lipsync bytes for ffmpeg — pull from R2 if not in scope.
    try:
        video_blob  # noqa: B018  — present iff we ran lipsync this tick
    except NameError:
        lip_obj = r2._client.get_object(Bucket=r2.bucket, Key=j.lipsync_key)
        video_blob = lip_obj["Body"].read()

    # ffmpeg insert: download lipsync mp4 + bottle jpg into tmp, apply
    # image cutaway at 10..13s, upload result, then concat with hook.
    import tempfile  # local import — keeps the cold-path stuff cold
    from pathlib import Path

    try:
        with tempfile.TemporaryDirectory(prefix="makeugc_") as tmpdir:
            tmp = Path(tmpdir)
            lipsync_tmp = tmp / "lipsync.mp4"
            bottle_tmp = tmp / "bottle.jpg"
            cutaway_tmp = tmp / "cutaway.mp4"
            final_tmp = tmp / "final.mp4"
            hook_tmp = tmp / "hook.mp4"

            lipsync_tmp.write_bytes(video_blob)
            bottle_tmp.write_bytes(bottle_blob)

            apply_image_cutaway(
                base_video=lipsync_tmp,
                insert_image=bottle_tmp,
                out_path=cutaway_tmp,
                start_seconds=10.0,
                end_seconds=13.0,
            )

            # --- Stage: CONCAT (hook + cutaway-augmented lipsync) ---
            _mark_stage(db, j, MakeUGCStatus.CONCAT)

            hook_obj = r2._client.get_object(
                Bucket=r2.bucket,
                Key="system/makeugc/hooks/bald_guy_aromaguru.mp4",
            )
            hook_tmp.write_bytes(hook_obj["Body"].read())

            concat_parts(
                parts=[hook_tmp, cutaway_tmp],
                out_path=final_tmp,
            )

            output_blob = final_tmp.read_bytes()
    except (CutawayError, ConcatError) as e:
        _fail(db, j, f"ffmpeg pipeline failed: {str(e)[:300]}")
        return
    except Exception as e:
        log.exception("makeugc %s ffmpeg unexpected fail", j.id)
        _fail(db, j, f"внутренняя ошибка concat: {str(e)[:200]}")
        return

    output_key = (
        f"users/{j.user_id}/makeugc/{j.id}/"
        f"final-{uuid.uuid4().hex[:6]}.mp4"
    )
    r2.upload_bytes(output_key, output_blob, content_type="video/mp4")
    j.output_key = output_key
    j.status = MakeUGCStatus.READY
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
                # Leave row's current status — next tick won't claim it
                # because SELECT FOR UPDATE SKIP LOCKED filters by
                # status=PENDING. Persistent transient = manual retry.
                db.rollback()
            except Exception as e:
                log.exception("makeugc %s hard fail", j.id)
                _fail(db, j, f"внутренняя ошибка: {str(e)[:200]}")
        finally:
            db.close()
