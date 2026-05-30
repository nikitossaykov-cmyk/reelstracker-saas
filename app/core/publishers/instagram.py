"""
Instagram Reels publisher через Graph API.

Reels publishing flow:
  POST /{ig-user-id}/media  (media_type=REELS, video_url, caption)
    → {"id": "<creation_id>"}
  Poll GET /{creation_id}?fields=status_code
    while status_code in ("IN_PROGRESS", "PUBLISHED" not yet),
    until "FINISHED" or "ERROR"
  POST /{ig-user-id}/media_publish (creation_id=<id>)
    → {"id": "<media_id>"}

Требует:
- Instagram BUSINESS или CREATOR account (личные не работают)
- Привязка к Facebook Page
- access_token с правами:
    instagram_basic, instagram_content_publish,
    pages_show_list, pages_read_engagement
- Длинноживущий (60 дней) token; refresh — отдельная задача (PR #10)

Видео-требования (актуальны на 2026-05-30):
- Длительность 3-90 сек для Reels
- Контейнер MP4, видео H.264, аудио AAC
- Aspect ratio 9:16 (рекомендуется), вертикалка
- Размер ≤ 1 GB
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from app.core.publishers.base import (
    PublisherBase, PublishResult, PublishError, OAuthExpired,
)

logger = logging.getLogger(__name__)


GRAPH_BASE = "https://graph.facebook.com/v21.0"
POLL_INTERVAL_SEC = 5
POLL_TIMEOUT_SEC = 300


class InstagramPublisher(PublisherBase):
    name = "instagram"

    def __init__(self, timeout: int = 60):
        self.timeout = timeout

    def _post(self, endpoint: str, params: dict, token: str) -> dict:
        import requests
        params = {**params, "access_token": token}
        url = f"{GRAPH_BASE}/{endpoint.lstrip('/')}"
        r = requests.post(url, data=params, timeout=self.timeout)
        return self._unwrap(r)

    def _get(self, endpoint: str, params: dict, token: str) -> dict:
        import requests
        params = {**params, "access_token": token}
        url = f"{GRAPH_BASE}/{endpoint.lstrip('/')}"
        r = requests.get(url, params=params, timeout=self.timeout)
        return self._unwrap(r)

    def _unwrap(self, r) -> dict:
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        if r.status_code >= 400:
            err = data.get("error") or {}
            msg = err.get("message") or str(data)[:300]
            code = err.get("code")
            # 190 / 102 — invalid/expired token
            if code in (190, 102) or r.status_code in (401, 403):
                raise OAuthExpired(f"Graph API auth {code}: {msg}")
            raise PublishError(f"Graph API {r.status_code} ({code}): {msg}")
        return data

    def publish_video(
        self,
        media_url: str,
        caption: str = "",
        *,
        platform_account_id: str,
        access_token: str,
    ) -> PublishResult:
        if not platform_account_id or not access_token:
            raise PublishError("platform_account_id и access_token обязательны")
        if not media_url:
            raise PublishError("media_url пуст")

        # 1. Create container
        logger.info(f"IG: create container for {media_url[:60]}...")
        create = self._post(
            f"{platform_account_id}/media",
            {
                "media_type": "REELS",
                "video_url": media_url,
                "caption": caption[:2200],  # IG лимит ~2200
                "share_to_feed": "true",
            },
            access_token,
        )
        creation_id = create.get("id")
        if not creation_id:
            raise PublishError(f"IG не вернул creation_id: {create}")

        # 2. Poll container status
        deadline = time.time() + POLL_TIMEOUT_SEC
        last_status: Optional[str] = None
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SEC)
            info = self._get(creation_id, {"fields": "status_code,status"},
                             access_token)
            sc = info.get("status_code") or info.get("status")
            if sc != last_status:
                logger.info(f"IG container {creation_id}: status={sc}")
                last_status = sc
            if sc == "FINISHED":
                break
            if sc in ("ERROR", "EXPIRED"):
                raise PublishError(
                    f"IG container failed: {sc} / {info.get('status')}"
                )
        else:
            raise PublishError(
                f"IG container poll timeout (>{POLL_TIMEOUT_SEC}s), status={last_status}"
            )

        # 3. Publish
        pub = self._post(
            f"{platform_account_id}/media_publish",
            {"creation_id": creation_id},
            access_token,
        )
        media_id = pub.get("id")
        if not media_id:
            raise PublishError(f"IG не вернул media_id: {pub}")

        # Permalink — отдельный GET (опционально для UX)
        permalink: Optional[str] = None
        try:
            perm = self._get(media_id, {"fields": "permalink"}, access_token)
            permalink = perm.get("permalink")
        except PublishError:
            pass

        logger.info(f"✅ IG published media_id={media_id}, permalink={permalink}")
        return PublishResult(
            platform_post_id=str(media_id),
            platform_url=permalink,
            raw_response={"creation_id": creation_id, "publish": pub},
        )
