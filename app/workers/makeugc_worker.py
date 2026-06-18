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

    # --- Stage: PORTRAIT ---
    _mark_stage(db, j, MakeUGCStatus.PORTRAIT)
    r2 = get_r2()

    try:
        obj = r2._client.get_object(Bucket=r2.bucket, Key=j.product_image_key)
        product_bytes = obj["Body"].read()
        product_ct = obj.get("ContentType") or "image/jpeg"

        url, cost = generate_portrait(
            product_image_bytes=product_bytes,
            product_content_type=product_ct,
            persona_style=j.persona_style,
            replicate_api_key=replicate_api_key,
            model=MODEL_MAX,
        )

        blob = download_bytes(url, timeout=120)
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

    # --- Stage: VOICEOVER (script gen + TTS) ---
    _mark_stage(db, j, MakeUGCStatus.VOICEOVER)

    openai_key = resolve_openai_key(user.openai_api_key)
    if not openai_key:
        _fail(db, j, "Нет OPENAI_API_KEY (ни у юзера, ни в env) — нужен для script gen")
        return
    eleven_key = resolve_eleven_key(user.elevenlabs_api_key)
    voice_id = resolve_voice_id(None)  # user-selectable voice arrives with UI
    if not eleven_key or not voice_id:
        _fail(db, j, "Нет ELEVENLABS_API_KEY или MAKEUGC_DEFAULT_VOICE_ID в env")
        return

    try:
        script_text = generate_script(
            product_name=j.product_name,
            premium_brand=j.premium_brand,
            premium_price_usd=float(j.premium_price_usd),
            mimic_price_usd=float(j.mimic_price_usd),
            persona_style=j.persona_style,
            openai_api_key=openai_key,
        )
    except Exception as e:
        log.exception("makeugc %s script-gen failed", j.id)
        _fail(db, j, f"script-gen ошибка: {str(e)[:200]}")
        return

    j.script_text = script_text
    db.commit()

    # Quota check on the shared key — only when user hasn't supplied own.
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

    # Persist audio in R2 + track cost + consume quota.
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

    # Lipsync / cutaway / concat are next-PR scope. Mark READY so the
    # UI can preview portrait + voiceover.
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
