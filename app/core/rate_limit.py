"""
Per-IP rate limiting via a custom Starlette middleware.

History: PR #4 tried slowapi 0.1.9 — the SlowAPIMiddleware silently
passed requests through on Starlette 1.x in prod (worked in TestClient).
This module replaces it with a zero-dep sliding-window limiter.

Storage: in-memory dict keyed by (path_rule, client_ip). Each entry is
a deque of request timestamps within the window. On every request we
drop expired timestamps, then count.

Rules: most specific path prefix wins. Each rule = (max_requests, window_seconds).
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


# (path_prefix, methods, max, window_sec)
# Longest prefix wins. Methods empty => all.
RULES: list[tuple[str, frozenset[str], int, int]] = [
    ("/api/auth/login",                 frozenset({"POST"}), 5,  60),
    ("/api/auth/register",              frozenset({"POST"}), 5,  60),
    ("/api/auth/refresh",               frozenset({"POST"}), 20, 60),
    ("/api/magic/from-url",             frozenset({"POST"}), 5,  60),
    ("/api/magic/from-upload",          frozenset({"POST"}), 5,  60),
    ("/api/account-insights/analyze",   frozenset({"POST"}), 5,  60),
    ("/api/forge/start",                frozenset({"POST"}), 5,  60),
    ("/api/parse",                      frozenset({"POST"}), 2,  60),
]
# Default safety net for everything else under /api/
DEFAULT_API_LIMIT = (100, 60)


def _client_ip(request: Request) -> str:
    # Railway forwards real IP in X-Forwarded-For (left-most). Fall back
    # to request.client.host if header absent (TestClient, direct calls).
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _match_rule(path: str, method: str) -> tuple[int, int]:
    best: tuple[str, int, int] | None = None
    for prefix, methods, n, win in RULES:
        if methods and method not in methods:
            continue
        if path == prefix or path.startswith(prefix + "/"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, n, win)
    if best:
        return best[1], best[2]
    if path.startswith("/api/"):
        return DEFAULT_API_LIMIT
    # Static, /, /forge — unlimited
    return (0, 0)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app) -> None:
        super().__init__(app)
        self._buckets: dict[tuple[str, str, str], deque[float]] = defaultdict(deque)
        self._lock = Lock()

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method
        n, window = _match_rule(path, method)
        if n == 0:
            return await call_next(request)

        ip = _client_ip(request)
        key = (path, method, ip)
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            q = self._buckets[key]
            while q and q[0] < cutoff:
                q.popleft()
            current = len(q)
            if current >= n:
                retry_after = max(1, int(q[0] + window - now))
                return JSONResponse(
                    {"detail": f"Rate limit exceeded: {n} per {window}s"},
                    status_code=429,
                    headers={
                        "Retry-After": str(retry_after),
                        "X-RateLimit-Limit": str(n),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int(now + retry_after)),
                    },
                )
            q.append(now)
            remaining = n - len(q)

        resp = await call_next(request)
        resp.headers["X-RateLimit-Limit"] = str(n)
        resp.headers["X-RateLimit-Remaining"] = str(remaining)
        return resp
