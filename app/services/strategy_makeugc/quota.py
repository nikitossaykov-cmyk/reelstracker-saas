"""Per-user monthly quota for the shared ElevenLabs key.

Cheap, idempotent helper called by the makeugc_worker before every
voiceover TTS call. Resets the counter when the calendar month rolls
over (compared to user.makeugc_quota_reset_at).

MVP limit: 6000 chars/month/user. One ~30-sec reel is ~300-400 chars
of Russian, so that's ~15-20 reels/user/month on the free tier. Tunable
via MAKEUGC_MONTHLY_CHAR_LIMIT env without redeploy.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.user import User


DEFAULT_MONTHLY_CHAR_LIMIT = 6000


def _monthly_limit() -> int:
    try:
        return int(os.getenv("MAKEUGC_MONTHLY_CHAR_LIMIT", str(DEFAULT_MONTHLY_CHAR_LIMIT)))
    except ValueError:
        return DEFAULT_MONTHLY_CHAR_LIMIT


def _start_of_next_month(now: datetime) -> datetime:
    if now.month == 12:
        return datetime(now.year + 1, 1, 1)
    return datetime(now.year, now.month + 1, 1)


def maybe_reset_counter(db: Session, user: User, *, now: Optional[datetime] = None) -> None:
    now = now or datetime.utcnow()
    reset_at = user.makeugc_quota_reset_at
    if reset_at is None or reset_at <= now:
        user.makeugc_chars_used_this_month = 0
        user.makeugc_quota_reset_at = _start_of_next_month(now)
        db.commit()


def has_budget(user: User, *, chars_needed: int) -> tuple[bool, int]:
    """Returns (is_ok, remaining_chars). Caller is responsible for
    maybe_reset_counter first."""
    limit = _monthly_limit()
    used = user.makeugc_chars_used_this_month or 0
    remaining = limit - used
    return chars_needed <= remaining, remaining


def consume(db: Session, user: User, *, chars: int) -> None:
    user.makeugc_chars_used_this_month = (
        (user.makeugc_chars_used_this_month or 0) + chars
    )
    db.commit()
