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
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    pass


def _download_via_apify(url: str, out_dir: Path,
                        apify_token: Optional[str] = None) -> tuple[Path, dict]:
    """Apify-actor download fallback for when yt-dlp gets IP-banned/login-walled.
    `apify_token` falls back to env var. Returns (file_path, meta) same as yt-dlp path.
    """
    import requests

    token = apify_token or os.environ.get("APIFY_API_TOKEN")
    if not token:
        raise DownloadError("no apify token (neither argument nor APIFY_API_TOKEN env)")

    platform = detect_platform(url)
    if platform == "tiktok":
        actor = "clockworks~tiktok-scraper"
        body = {"postURLs": [url], "shouldDownloadVideos": True, "resultsPerPage": 1}
    elif platform == "instagram":
        actor = "apify~instagram-scraper"
        body = {"directUrls": [url], "resultsType": "details", "resultsLimit": 1}
    else:
        raise DownloadError(f"apify fallback не поддерживает платформу: {platform}")

    logger.info(f"apify fallback: {actor} for {platform}")
    r = requests.post(
        f"https://api.apify.com/v2/acts/{actor}/runs?token={token}",
        json=body, timeout=60,
    )
    if r.status_code >= 400:
        raise DownloadError(f"apify start HTTP {r.status_code}: {r.text[:200]}")
    run = r.json().get("data") or {}
    run_id = run.get("id")
    if not run_id:
        raise DownloadError("apify: no run id in response")

    # poll up to 180s
    status = run.get("status")
    for _ in range(60):
        time.sleep(3)
        rr = requests.get(
            f"https://api.apify.com/v2/actor-runs/{run_id}?token={token}",
            timeout=30,
        )
        data = rr.json().get("data") or {}
        status = data.get("status")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
    if status != "SUCCEEDED":
        raise DownloadError(f"apify run finished as {status}")

    dataset_id = data.get("defaultDatasetId")
    if not dataset_id:
        raise DownloadError("apify: no defaultDatasetId")
    items_url = (f"https://api.apify.com/v2/datasets/{dataset_id}/items"
                 f"?token={token}&clean=true&format=json")
    items = requests.get(items_url, timeout=60).json()
    if not items:
        raise DownloadError("apify dataset empty — actor returned no items")
    item = items[0]

    # Different actors expose the MP4 URL under different keys.
    video_url = (
        item.get("videoUrl")
        or item.get("videoUrlNoWaterMark")
        or (item.get("videoMeta") or {}).get("downloadAddr")
        or item.get("mediaUrl")
        or (item.get("videoVersions") or [{}])[0].get("url")
    )
    if not video_url:
        actor_err = item.get("error") or item.get("errorMessage") or ""
        actor_code = item.get("errorCode") or ""
        if actor_err or actor_code:
            raise DownloadError(
                f"apify actor reported error: {actor_code} {actor_err[:200]}"
            )
        raise DownloadError(
            f"apify: no video url in item; keys={list(item.keys())[:15]}"
        )

    out = out_dir / "video.mp4"
    with requests.get(video_url, stream=True, timeout=180) as resp:
        resp.raise_for_status()
        with out.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    size = out.stat().st_size
    if size < 10_000:
        raise DownloadError(f"apify-downloaded file too small: {size} bytes")

    meta = {
        "title": (item.get("text") or item.get("caption") or "")[:255],
        "uploader": (item.get("authorMeta") or {}).get("name") or item.get("ownerUsername"),
        "uploader_id": item.get("authorId") or item.get("ownerId"),
        "duration": (item.get("videoMeta") or {}).get("duration") or item.get("videoDuration"),
        "view_count": item.get("playCount") or item.get("videoPlayCount"),
        "like_count": item.get("diggCount") or item.get("likesCount"),
        "comment_count": item.get("commentCount"),
        "webpage_url": url,
        "thumbnail": (item.get("videoMeta") or {}).get("coverUrl") or item.get("displayUrl"),
        "platform": platform,
        "description": (item.get("text") or "")[:2000],
        "downloader": "apify",
    }
    logger.info(f"apify: downloaded {size//1024} KB from {platform}")
    return out, meta


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


def download_video(url: str, out_dir: Optional[Path] = None,
                   apify_token: Optional[str] = None) -> tuple[Path, dict]:
    """Скачать MP4 по URL. Возвращает (path, metadata).

    metadata содержит как минимум: title, uploader, duration, view_count,
    like_count (если платформа отдала), webpage_url, thumbnail.

    `apify_token` — пользовательский apify api-key. Если задан и yt-dlp
    падает с IP-block / login-required, автоматический fallback на
    соответствующий Apify scraper actor. Без него — fallback на env var
    APIFY_API_TOKEN. Без обоих — никакого fallback.
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
        # When yt-dlp gets blocked by IP / login-walled, try Apify scraper
        # fallback (requires APIFY_API_TOKEN in env). Covers both IG 401
        # and the TT "Your IP address is blocked" pattern that hit Nick.
        ip_blocked = (
            "rate-limit" in low or "login required" in low or "cookies" in low
            or "ip address is blocked" in low or "ip blocked" in low
            or "not available" in low
        )
        effective_token = apify_token or os.environ.get("APIFY_API_TOKEN")
        token_present = bool(effective_token)
        logger.warning(
            f"yt-dlp failed: ip_blocked={ip_blocked} apify_token_present={token_present} "
            f"err={err_str[:200]}"
        )
        if ip_blocked and token_present:
            try:
                logger.warning(f"trying Apify fallback for {url}")
                return _download_via_apify(url, workdir, apify_token=effective_token)
            except DownloadError as e2:
                raise DownloadError(
                    f"yt-dlp blocked + Apify fallback failed: {e2}"
                )
        if ip_blocked and not token_present:
            raise DownloadError(
                "yt-dlp blocked AND no Apify token available. Add an "
                "apify_token to your profile via /api/settings/apify "
                f"or set APIFY_API_TOKEN env. yt-dlp: {err_str[:200]}"
            )
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
