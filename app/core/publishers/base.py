"""
Абстракция publisher'а — общий контракт для всех платформ.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class PublishResult:
    platform_post_id: str
    platform_url: Optional[str] = None
    raw_response: Optional[dict] = None


class PublishError(Exception):
    """Generic ошибка при публикации."""


class OAuthExpired(PublishError):
    """Access token истёк / отозван — нужен refresh."""


class PublisherBase(ABC):
    name: str = "base"

    @abstractmethod
    def publish_video(
        self,
        media_url: str,
        caption: str = "",
        *,
        platform_account_id: str,
        access_token: str,
    ) -> PublishResult:
        """Опубликовать видео по публичному URL.

        - `media_url` — публичная ссылка на MP4 (presigned R2 / CDN).
          Должна быть достижима из datacenter платформы минимум 24ч.
        - `caption` — текст подписи (включая хэштеги).
        """
        ...
