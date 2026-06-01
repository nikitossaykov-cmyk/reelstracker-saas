"""
slowapi-based rate limiting. Per-IP, in-memory (single Railway dyno).

Limits:
  AUTH       — 5/minute   (brute-force on login, register flood)
  REFRESH    — 20/minute  (refresh is more frequent but still bounded)
  EXPENSIVE  — 5/minute   (Magic Mode, account-insights — each costs cents)
  PARSE      — 2/minute   (Apify-backed parse trigger; per-user lock is separate)
  DEFAULT    — 100/minute (everything else, mostly read endpoints)

Railway terminates TLS upstream, so remote_address from request.client.host
is the proxy IP. We honour X-Forwarded-For via uvicorn --proxy-headers
(already on by default in our config).
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address


limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["100/minute"],
    headers_enabled=True,
)

AUTH = "5/minute"
REFRESH = "20/minute"
EXPENSIVE = "5/minute"
PARSE = "2/minute"
