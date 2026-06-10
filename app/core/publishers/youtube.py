"""
YouTube Shorts publisher через Data API v3.

YouTube Shorts = обычный YouTube upload, но с длиной ≤60с + вертикалкой.
Никакого специального endpoint'a нет, просто videos.insert с #shorts в
title/description чтобы попасть в Shorts feed.

Flow:
  1. resumable POST init → upload_url + sessionURI
  2. PUT video bytes на upload_url
  3. response: id, title, status

Требует:
- Google OAuth 2.0 access_token со scope https://www.googleapis.com/auth/youtube.upload
- Google Cloud project с включенным YouTube Data API v3

Refresh long-lived = refresh_token (offline access).
"""

from __future__ import annotations

import logging

from app.core.publishers.base import (
    PublisherBase, PublishResult, PublishError, OAuthExpired,
)

logger = logging.getLogger(__name__)


UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3/videos"
API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubePublisher(PublisherBase):
    name = "youtube_shorts"

    def __init__(self,
                 category_id: str = "22",  # "People & Blogs" default
                 privacy: str = "public",  # private / unlisted / public
                 timeout: int = 60):
        self.category_id = category_id
        self.privacy = privacy
        self.timeout = timeout

    def publish_video(
        self,
        media_url: str,
        caption: str = "",
        *,
        platform_account_id: str,  # YouTube channel ID (необязательно для upload)
        access_token: str,
    ) -> PublishResult:
        import requests

        # 1. Подготовить metadata
        title = (caption or "Generated Short").split("\n")[0][:100]
        if "#shorts" not in title.lower() and "#shorts" not in (caption or "").lower():
            title = (title[:90] + " #shorts").strip()
        body = {
            "snippet": {
                "title": title,
                "description": (caption or "")[:5000],
                "categoryId": self.category_id,
                "tags": ["shorts"],
            },
            "status": {
                "privacyStatus": self.privacy,
                "selfDeclaredMadeForKids": False,
            },
        }

        # 2. Скачать media — YouTube requires direct binary upload (no PULL_FROM_URL)
        try:
            r = requests.get(media_url, stream=True, timeout=180)
            r.raise_for_status()
            video_bytes = r.content
        except Exception as e:
            raise PublishError(f"YouTube source download failed: {e}")

        # 3. Resumable upload init
        init = requests.post(
            UPLOAD_BASE,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(len(video_bytes)),
            },
            json=body,
            timeout=self.timeout,
        )
        if init.status_code in (401, 403):
            raise OAuthExpired(f"YouTube auth {init.status_code}: {init.text[:200]}")
        if init.status_code >= 400:
            raise PublishError(f"YouTube init HTTP {init.status_code}: {init.text[:300]}")
        upload_url = init.headers.get("Location") or init.headers.get("location")
        if not upload_url:
            raise PublishError(f"YouTube init: no Location header. body={init.text[:200]}")

        # 4. PUT bytes
        put = requests.put(
            upload_url,
            data=video_bytes,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "video/mp4",
                "Content-Length": str(len(video_bytes)),
            },
            timeout=600,
        )
        if put.status_code >= 400:
            raise PublishError(f"YouTube PUT HTTP {put.status_code}: {put.text[:300]}")
        try:
            data = put.json()
        except Exception:
            raise PublishError(f"YouTube non-JSON PUT response: {put.text[:300]}")

        video_id = data.get("id")
        if not video_id:
            raise PublishError(f"YouTube no id in response: {data}")
        permalink = f"https://www.youtube.com/shorts/{video_id}"
        logger.info(f"YouTube Shorts published: {permalink}")
        return PublishResult(
            platform_post_id=video_id,
            platform_url=permalink,
            raw_response=data,
        )
