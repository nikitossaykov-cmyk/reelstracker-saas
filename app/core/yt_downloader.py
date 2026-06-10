"""
yt-dlp обёртка для скачивания MP4 из IG/TikTok/YouTube/Reels URL'ов.

Используется Magic Mode'ом, чтобы юзер просто кинул ссылку «вот у
конкурента залетел» — а мы вытащили MP4 без Apify-токена / OAuth /
прочих сложностей.

Лимиты:
- IG требует cookies для приватных аккаунтов; публичные обычно ОК
- TikTok иногда ругается на watermark — yt-dlp пытается достать
  no-watermark вариант через cdn-ы
- YouTube Shorts — без проблем
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    pass


def detect_platform(url: str) -> str:
    u = url.lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u or "instagr.am" in u:
        return "instagram"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "vk.com" in u or "vk.ru" in u:
        return "vk"
    return "unknown"


def download_video(url: str, out_dir: Optional[Path] = None) -> tuple[Path, dict]:
    """Скачать MP4 по URL. Возвращает (path, metadata).

    metadata содержит как минимум: title, uploader, duration, view_count,
    like_count (если платформа отдала), webpage_url, thumbnail.
    """
    try:
        import yt_dlp
    except ImportError as e:
        raise DownloadError(f"yt-dlp не установлен: {e}")

    workdir = out_dir or Path(tempfile.mkdtemp(prefix=f"ytdl_{uuid.uuid4().hex[:8]}_"))
    workdir.mkdir(parents=True, exist_ok=True)
    out_template = str(workdir / "video.%(ext)s")

    opts = {
        "format": "best[ext=mp4]/best[height<=1080]/best",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "socket_timeout": 60,
        "retries": 3,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        err_str = str(e)
        low = err_str.lower()
        # IG with no cookies regularly returns "rate-limit reached or login
        # required" — surface a friendlier message and a workaround.
        if "instagram" in url.lower() and (
            "rate-limit" in low or "login required" in low or "cookies" in low
        ):
            raise DownloadError(
                "Instagram заблокировал скачивание (rate-limit / нужны cookies). "
                "Скачай ролик вручную через любой ig-downloader и попробуй стратегию A "
                "с загрузкой MP4-файла, либо найди тот же контент в TikTok/YouTube Shorts."
            )
        raise DownloadError(f"yt-dlp {type(e).__name__}: {err_str[:300]}")

    # Найти скачанный файл
    mp4s = list(workdir.glob("video.*"))
    mp4s = [p for p in mp4s if p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov")]
    if not mp4s:
        raise DownloadError(f"yt-dlp finished but no mp4 in {workdir}")

    file = mp4s[0]
    # Sanity check: yt-dlp occasionally writes a 0-byte / few-byte stub
    # when IG returns a degraded response, which then breaks ffmpeg
    # downstream and surfaces as 'empty result' in the UI.
    size = file.stat().st_size
    if size < 10_000:  # <10KB → definitely not a real reel
        raise DownloadError(
            f"yt-dlp скачал {size} байт — это не валидный MP4. "
            "Скорее всего платформа отдала заглушку (особенно частая проблема "
            "у Instagram). Попробуй TikTok/YouTube ссылку или загрузи MP4 вручную."
        )
    meta = {
        "title": (info.get("title") or "")[:255],
        "uploader": info.get("uploader") or info.get("channel"),
        "uploader_id": info.get("uploader_id") or info.get("channel_id"),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "webpage_url": info.get("webpage_url") or url,
        "thumbnail": info.get("thumbnail"),
        "platform": detect_platform(url),
        "description": (info.get("description") or "")[:2000],
    }
    logger.info(f"yt-dlp: downloaded {file.name} ({file.stat().st_size//1024} KB) from {meta['platform']}")
    return file, meta
