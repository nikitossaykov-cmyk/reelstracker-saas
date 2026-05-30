"""
VK Clips publisher через VK API.

Flow:
  1. GET shortVideo.create или video.save  → upload_url
  2. POST multipart-upload файла на upload_url → video_id, owner_id
  3. (опционально) GET wall.post чтобы прикрепить к стене

Использует video.save + clip-параметр для Clips формата (вертикалка ≤60с).
Требует access_token со scope `video,wall` + group_id или user_id.

API доcs: dev.vk.com/method/video.save
"""

from __future__ import annotations

import logging
from typing import Optional

from app.core.publishers.base import (
    PublisherBase, PublishResult, PublishError, OAuthExpired,
)

logger = logging.getLogger(__name__)


API_BASE = "https://api.vk.com/method"
API_VERSION = "5.199"


class VKPublisher(PublisherBase):
    name = "vk_clips"

    def __init__(self,
                 group_id: Optional[int] = None,  # если постим в группу
                 timeout: int = 60):
        self.group_id = group_id
        self.timeout = timeout

    def _vk(self, method: str, params: dict, token: str) -> dict:
        import requests
        params = {**params, "access_token": token, "v": API_VERSION}
        url = f"{API_BASE}/{method}"
        r = requests.post(url, data=params, timeout=self.timeout)
        try:
            data = r.json()
        except Exception:
            raise PublishError(f"VK {method}: non-JSON response: {r.text[:300]}")
        if "error" in data:
            err = data["error"]
            code = err.get("error_code")
            msg = err.get("error_msg", "")
            if code in (5, 15, 27, 28):  # invalid/expired token
                raise OAuthExpired(f"VK auth: {code}: {msg}")
            raise PublishError(f"VK {method} error {code}: {msg}")
        return data.get("response", {})

    def publish_video(
        self,
        media_url: str,
        caption: str = "",
        *,
        platform_account_id: str,  # для VK = user_id или group_id (числовой)
        access_token: str,
    ) -> PublishResult:
        # 1. Получить upload_url
        save_params = {
            "name": (caption or "")[:255],
            "description": (caption or "")[:1000],
            "is_private": 0,
            "wallpost": 0,
            "no_comments": 0,
            "compression": 1,
        }
        if self.group_id:
            save_params["group_id"] = self.group_id
        info = self._vk("video.save", save_params, access_token)
        upload_url = info.get("upload_url")
        if not upload_url:
            raise PublishError(f"VK video.save без upload_url: {info}")
        video_id = info.get("video_id")
        owner_id = info.get("owner_id") or (-self.group_id if self.group_id else None)

        # 2. Скачать file → upload как multipart
        import requests
        try:
            with requests.get(media_url, stream=True, timeout=180) as src:
                src.raise_for_status()
                content = src.content
            up = requests.post(upload_url,
                               files={"video_file": ("video.mp4", content, "video/mp4")},
                               timeout=600)
            up.raise_for_status()
            up_data = up.json()
        except Exception as e:
            raise PublishError(f"VK upload failed: {e}")

        if up_data.get("error_code"):
            raise PublishError(f"VK upload returned error: {up_data}")
        final_video_id = up_data.get("video_id", video_id)
        final_owner_id = up_data.get("owner_id", owner_id)
        permalink = (f"https://vk.com/video{final_owner_id}_{final_video_id}"
                     if final_owner_id and final_video_id else None)
        logger.info(f"VK published: {permalink}")
        return PublishResult(
            platform_post_id=f"{final_owner_id}_{final_video_id}",
            platform_url=permalink,
            raw_response=up_data,
        )
