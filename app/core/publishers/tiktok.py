"""
TikTok publisher через Content Posting API (Open Platform).

Flow (PULL_FROM_URL вариант — TikTok сам качает наш R2 URL):
  1. POST /v2/post/publish/video/init  → {publish_id}
       body: {source_info: {source: PULL_FROM_URL, video_url: ...},
              post_info: {title, privacy_level, ...}}
  2. Poll GET /v2/post/publish/status/fetch/?publish_id=<id>
       until status in {PUBLISH_COMPLETE, FAILED}

Требует:
- TikTok for Business / TikTok Developer App с unaudited mode (SANDBOX) или
  audited app с правами video.publish + video.upload
- access_token с правом video.publish (long-lived: 24h, refresh 365 дней)
- domain verification на нашем R2 endpoint (через .well-known/tiktok-developers/)
- video длительностью 3-60 сек, MP4, H.264 + AAC, ≤500MB

Note: PULL_FROM_URL требует verified domain. Если у нас R2 без custom CDN
— нужен второй flow FILE_UPLOAD (multipart). Пока даём оба, юзер выбирает.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from app.core.publishers.base import (
    PublisherBase, PublishResult, PublishError, OAuthExpired,
)

logger = logging.getLogger(__name__)


API_BASE = "https://open.tiktokapis.com"
POLL_INTERVAL_SEC = 5
POLL_TIMEOUT_SEC = 300


class TikTokPublisher(PublisherBase):
    name = "tiktok"

    def __init__(self,
                 source_mode: Literal["PULL_FROM_URL", "FILE_UPLOAD"] = "PULL_FROM_URL",
                 privacy_level: str = "PUBLIC_TO_EVERYONE",
                 timeout: int = 60):
        self.source_mode = source_mode
        self.privacy_level = privacy_level
        self.timeout = timeout

    def _post(self, endpoint: str, body: dict, token: str) -> dict:
        import requests
        url = f"{API_BASE}/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        r = requests.post(url, json=body, headers=headers, timeout=self.timeout)
        return self._unwrap(r)

    def _get(self, endpoint: str, params: dict, token: str) -> dict:
        import requests
        url = f"{API_BASE}/{endpoint.lstrip('/')}"
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(url, params=params, headers=headers, timeout=self.timeout)
        return self._unwrap(r)

    def _unwrap(self, r) -> dict:
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        if r.status_code in (401, 403):
            raise OAuthExpired(f"TikTok auth {r.status_code}: {data}")
        if r.status_code >= 400:
            raise PublishError(f"TikTok HTTP {r.status_code}: {str(data)[:300]}")
        err = (data.get("error") or {}).get("code")
        if err and err not in ("ok",):
            msg = (data.get("error") or {}).get("message", "")
            if err in ("access_token_invalid", "scope_not_authorized"):
                raise OAuthExpired(f"TikTok {err}: {msg}")
            raise PublishError(f"TikTok {err}: {msg}")
        return data

    def publish_video(
        self,
        media_url: str,
        caption: str = "",
        *,
        platform_account_id: str,  # for TikTok = open_id (но pas obligatoire здесь)
        access_token: str,
    ) -> PublishResult:
        if self.source_mode != "PULL_FROM_URL":
            raise PublishError(
                "FILE_UPLOAD пока не реализован — используй PULL_FROM_URL "
                "(требует verified domain на R2 endpoint)"
            )
        body = {
            "post_info": {
                "title": (caption or "")[:2200],
                "privacy_level": self.privacy_level,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
                "video_cover_timestamp_ms": 1000,
            },
            "source_info": {
                "source": "PULL_FROM_URL",
                "video_url": media_url,
            },
        }
        init = self._post("v2/post/publish/video/init/", body, access_token)
        publish_id = (init.get("data") or {}).get("publish_id")
        if not publish_id:
            raise PublishError(f"TikTok init без publish_id: {init}")
        logger.info(f"TikTok publish_id={publish_id}")

        # Poll
        deadline = time.time() + POLL_TIMEOUT_SEC
        last_status = None
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SEC)
            st = self._post("v2/post/publish/status/fetch/",
                            {"publish_id": publish_id}, access_token)
            data = st.get("data") or {}
            status = data.get("status")
            if status != last_status:
                logger.info(f"TikTok publish_id={publish_id} status={status}")
                last_status = status
            if status == "PUBLISH_COMPLETE":
                publicaly_available_post_id = (data.get("publicaly_available_post_id") or
                                               data.get("publicly_available_post_id"))  # API typo на ранних версиях
                post_ids = publicaly_available_post_id or []
                video_id = post_ids[0] if isinstance(post_ids, list) and post_ids else publish_id
                return PublishResult(
                    platform_post_id=str(video_id),
                    platform_url=None,  # TikTok permalink требует open_id + video_id; опционально
                    raw_response=data,
                )
            if status in ("FAILED",):
                raise PublishError(f"TikTok publish FAILED: {data.get('fail_reason', data)}")
        raise PublishError(f"TikTok publish timeout, last status={last_status}")
