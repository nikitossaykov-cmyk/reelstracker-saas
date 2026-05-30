"""
Cross-platform publishers — общий интерфейс для IG/TT/YT/VK.

PR #9 содержит только Instagram. TikTok/YT/VK — extensions поверх
PublisherBase в следующих PR / по мере получения API доступа.
"""

from app.core.publishers.base import (
    PublisherBase, PublishResult, PublishError, OAuthExpired,
)
from app.core.publishers.instagram import InstagramPublisher
from app.core.publishers.tiktok import TikTokPublisher
from app.core.publishers.vk import VKPublisher
from app.core.publishers.youtube import YouTubePublisher

__all__ = [
    "PublisherBase", "PublishResult", "PublishError", "OAuthExpired",
    "InstagramPublisher", "TikTokPublisher", "VKPublisher", "YouTubePublisher",
    "get_publisher",
]


def get_publisher(platform: str, **kwargs) -> PublisherBase:
    if platform == "instagram":
        return InstagramPublisher(**{k: v for k, v in kwargs.items() if k == "timeout"})
    if platform == "tiktok":
        return TikTokPublisher(**{k: v for k, v in kwargs.items() if k in
                                  ("source_mode", "privacy_level", "timeout")})
    if platform in ("vk", "vk_clips"):
        return VKPublisher(**{k: v for k, v in kwargs.items() if k in
                              ("group_id", "timeout")})
    if platform in ("youtube", "youtube_shorts"):
        return YouTubePublisher(**{k: v for k, v in kwargs.items() if k in
                                   ("category_id", "privacy", "timeout")})
    raise NotImplementedError(f"Publisher для платформы '{platform}' не реализован")
