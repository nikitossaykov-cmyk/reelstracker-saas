"""
Cross-platform publishers — общий интерфейс для IG/TT/YT/VK.

PR #9 содержит только Instagram. TikTok/YT/VK — extensions поверх
PublisherBase в следующих PR / по мере получения API доступа.
"""

from app.core.publishers.base import (
    PublisherBase, PublishResult, PublishError, OAuthExpired,
)
from app.core.publishers.instagram import InstagramPublisher

__all__ = [
    "PublisherBase", "PublishResult", "PublishError", "OAuthExpired",
    "InstagramPublisher",
    "get_publisher",
]


def get_publisher(platform: str, **kwargs) -> PublisherBase:
    if platform == "instagram":
        return InstagramPublisher(**kwargs)
    raise NotImplementedError(f"Publisher для платформы '{platform}' не реализован")
