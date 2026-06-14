"""Drains 'personas' rows in GENERATING state via Replicate PuLID-Flux.

For each pending row:
  1. Build a moderation-safe prompt (safety frame BEFORE user bio).
  2. Call Replicate with the user's own replicate_api_key, 4 seeds.
  3. On safety reject → one defensive-fallback retry, then mark FAILED.
  4. Download each candidate and re-upload to R2 for stable URLs.
  5. Persist gallery_json + status=AWAITING_CANONICAL.

Row locking is via SELECT ... FOR UPDATE SKIP LOCKED so the drain loop
is safe to run from multiple processes once we extract it out of the
web process (memory: Forge tech insights priority 1).
"""
from __future__ import annotations

import logging
import time
import uuid

from sqlalchemy.orm import Session

from app.models.persona import Persona, PersonaStatus
from app.services.persona_prompt import (
    build_persona_prompt,
    defensive_fallback_prompt,
)
from app.services.replicate_client import (
    ReplicateClient,
    ReplicateError,
    ReplicateSafetyError,
    ReplicateTransientError,
)
from app.services.media_helpers import download_bytes
from app.core.storage import get_r2


log = logging.getLogger(__name__)


REPLICATE_MODEL = "lucataco/pulid-flux"
SEEDS = [101, 202, 303, 404]


def pick_next_pending(db: Session) -> Persona | None:
    return (
        db.query(Persona)
        .filter(
            Persona.status == PersonaStatus.GENERATING,
            Persona.deleted_at.is_(None),
        )
        .order_by(Persona.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )


def _fail(db: Session, p: Persona, msg: str) -> None:
    p.status = PersonaStatus.FAILED
    p.error_message = msg[:500]
    db.commit()


def process_persona(
    db: Session, p: Persona, replicate_api_key: str
) -> None:
    """One full Replicate call cycle for a single persona row.

    Outer loop handles SKIP-LOCKED claim and ReplicateTransientError
    (which means: leave the row in GENERATING, try again next tick).
    """
    if not replicate_api_key:
        _fail(db, p, "У пользователя нет Replicate API key — задай его в /settings")
        return

    client = ReplicateClient(api_key=replicate_api_key)
    primary = build_persona_prompt(p.bio, p.style_hint)

    def _gen_one(prompt_text: str, seed: int) -> str | None:
        out = client.run_model(
            REPLICATE_MODEL,
            {
                "prompt": prompt_text,
                "seed": seed,
                "num_outputs": 1,
                "aspect_ratio": "9:16",
            },
        )
        if isinstance(out, str):
            return out
        urls = list(out) if out else []
        return urls[0] if urls else None

    def _gen_all(prompt_text: str) -> list[dict]:
        out: list[dict] = []
        for idx, seed in enumerate(SEEDS):
            url = _gen_one(prompt_text, seed)
            if url:
                out.append({"url": url, "seed": seed, "index": idx})
        return out

    try:
        candidates = _gen_all(primary)
    except ReplicateSafetyError:
        log.info("persona %s primary prompt safety-rejected, trying fallback", p.id)
        try:
            candidates = _gen_all(defensive_fallback_prompt())
        except ReplicateSafetyError:
            _fail(
                db, p,
                "🛑 OpenAI/Replicate moderation отбила и основной prompt, и "
                "fallback. Перепиши `bio` более общо — без меток возраста "
                "('young', 'teen') и без описаний реальных людей.",
            )
            return
        except ReplicateTransientError as e:
            log.warning("persona %s transient on fallback: %s", p.id, e)
            raise  # outer loop leaves row in GENERATING for retry
    except ReplicateTransientError as e:
        log.warning("persona %s transient on primary: %s", p.id, e)
        raise
    except ReplicateError as e:
        _fail(db, p, f"Replicate hard fail: {str(e)[:200]}")
        return

    if not candidates:
        _fail(db, p, "Replicate вернул пусто на все 4 seed-а")
        return

    # Re-host candidate images on our R2 so URLs don't expire on Replicate's CDN.
    r2 = get_r2()
    gallery: list[dict] = []
    for c in candidates:
        try:
            blob = download_bytes(c["url"], timeout=60)
        except Exception as e:
            log.warning(
                "persona %s seed %s download failed: %s", p.id, c["seed"], e
            )
            continue
        key = (
            f"users/{p.user_id}/personas/{p.id}/"
            f"{c['seed']}-{uuid.uuid4().hex[:6]}.png"
        )
        r2.upload_bytes(key, blob, content_type="image/png")
        gallery.append(
            {"url": r2.get_proxy_url(key), "seed": c["seed"], "index": c["index"]}
        )

    if not gallery:
        _fail(db, p, "Все кандидаты не скачались с Replicate")
        return

    p.gallery_json = gallery
    p.status = PersonaStatus.AWAITING_CANONICAL
    db.commit()


def run_loop(db_factory, poll_seconds: float = 2.0) -> None:
    """Long-running drain loop. db_factory() returns a fresh Session per tick."""
    while True:
        db = db_factory()
        try:
            p = pick_next_pending(db)
            if p is None:
                db.commit()
                time.sleep(poll_seconds)
                continue
            try:
                from app.models.user import User
                user = db.query(User).get(p.user_id)
                api_key = user.replicate_api_key if user else None
                process_persona(db, p, replicate_api_key=api_key or "")
            except ReplicateTransientError:
                # leave row in GENERATING — next tick will retry
                db.rollback()
            except Exception as e:
                log.exception("persona %s hard fail", p.id)
                _fail(db, p, f"внутренняя ошибка: {str(e)[:200]}")
        finally:
            db.close()
