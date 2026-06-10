"""
Account Analyzer — берёт URL аккаунта (TikTok / IG), скачивает топ-N
рилсов через yt-dlp, для каждого делает упрощённый анализ (vision-only,
без полного analyzer worker pipeline чтоб не плодить долгие jobs), и
LLM-aggregate'ит в единый summary об аккаунте:
  - top hook types
  - recurring visuals
  - продукт-ниша (если узнаётся)
  - длительности
  - production style
  - format breakdown

Это «осмотр аккаунта за 2 минуты» — потом юзер может одним кликом
сделать ремейк любого из найденных рилсов.

Стоимость: ~$0.01 на аккаунт (10 рилсов × ~$0.001/vision summary).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


class AccountAnalyzeError(Exception):
    pass


def _normalize_account_url(input_str: str) -> str:
    """Принимает @username / username / полную URL → возвращает каноническую
    URL аккаунта. Эвристика."""
    s = input_str.strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s
    s = s.lstrip("@")
    # IG default
    return f"https://www.instagram.com/{s}/"


def _fetch_recent_reels(account_url: str, max_reels: int = 10) -> list[dict]:
    """Через yt-dlp получить metadata топ-N рилсов аккаунта (без скачивания).

    yt-dlp поддерживает IG playlist-mode для аккаунтов, но нужны
    "channel"-style URL. Для IG обычно `instagram.com/<username>/reels/`.
    """
    try:
        import yt_dlp
    except ImportError as e:
        raise AccountAnalyzeError(f"yt-dlp not installed: {e}")

    # Для IG юзер-URL дёргаем /reels/ tab
    url = account_url.rstrip("/")
    if "instagram.com" in url and not url.endswith("/reels"):
        url = url + "/reels"

    opts = {
        "quiet": True, "no_warnings": True,
        "extract_flat": True,  # только metadata, без download
        "playlistend": max_reels,
        "skip_download": True,
        "socket_timeout": 60,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise AccountAnalyzeError(f"yt-dlp failed for {url}: {str(e)[:200]}")

    entries = info.get("entries") or []
    if not entries:
        # Try fallback to bare profile URL
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(account_url, download=False)
            entries = info.get("entries") or []
        except Exception as e:
            raise AccountAnalyzeError(f"no reels found via yt-dlp ({e})")

    reels: list[dict] = []
    for e in entries[:max_reels]:
        if not isinstance(e, dict):
            continue
        reels.append({
            "url": e.get("url") or e.get("webpage_url"),
            "title": e.get("title") or "",
            "duration": e.get("duration"),
            "view_count": e.get("view_count"),
            "like_count": e.get("like_count"),
            "comment_count": e.get("comment_count"),
            "thumbnail": e.get("thumbnail"),
            "upload_date": e.get("upload_date"),
        })
    return reels


def aggregate_account_insights(
    account_url: str,
    reels: list[dict],
    *,
    openai_api_key: str,
    timeout: int = 60,
) -> dict:
    """Передать metadata всех рилсов в LLM и попросить unified summary.

    Без скачивания каждого рилса (дорого и медленно) — основываемся на
    title / view_count / duration / thumbnail. Для глубокого разбора
    юзер потом делает Magic Mode на интересном рилсе.
    """
    try:
        from openai import OpenAI
    except ImportError as e:
        raise AccountAnalyzeError(f"openai SDK not installed: {e}")
    client = OpenAI(api_key=openai_api_key, timeout=timeout)

    reels_compact = []
    for r in reels[:20]:
        reels_compact.append({
            "title": (r.get("title") or "")[:120],
            "duration": r.get("duration"),
            "views": r.get("view_count"),
            "likes": r.get("like_count"),
            "comments": r.get("comment_count"),
        })

    sys = """\
You analyse a list of short-form video metadata (TikTok or Instagram Reels)
from ONE creator's account and produce a structured insight summary.

Output STRICT JSON only (no fences). Schema:
{
  "niche": "ru-perfume|en-beauty|lifestyle|fitness|... best-guess content niche",
  "language": "ru|en|...",
  "creator_persona": "short description of who this creator is and their angle",
  "common_hooks": ["POV — most common", "DUPE — second", ...] (≤4 items),
  "recurring_visual_motifs": ["close-up of bottle", "bedroom selfie", ...] (≤5 items),
  "format_summary": "How videos are structured (e.g. 'TWIST: emotional opening → product reveal → CTA')",
  "avg_duration_sec": float,
  "publishing_cadence": "your guess from upload dates (daily/weekly/...)",
  "performance": {
    "top_view_count": integer,
    "median_view_count": integer,
    "top_performing_titles": ["...", "..."] (≤3)
  },
  "remake_recommendation": "Which 1-2 videos are most worth remaking and why (be specific by title)"
}
Be specific, don't generic-pad. If you can't guess a field, set null.
"""
    user = "ACCOUNT URL: " + account_url + "\n\nRECENT REELS:\n" + json.dumps(reels_compact, ensure_ascii=False, indent=1)

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            max_tokens=1500,
            temperature=0.3,
        )
    except Exception as e:
        raise AccountAnalyzeError(f"OpenAI {type(e).__name__}: {str(e)[:200]}")

    raw = (resp.choices[0].message.content or "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"raw_text": raw}
    return parsed


def analyze_account(account_input: str, openai_api_key: str,
                    max_reels: int = 10) -> dict:
    """Полный pipeline: input → URL → fetch reels metadata → LLM summary.

    Возвращает {account_url, reels: [...], insights: {...}}.
    """
    account_url = _normalize_account_url(account_input)
    logger.info(f"📊 analyze_account: {account_url}")
    reels = _fetch_recent_reels(account_url, max_reels=max_reels)
    if not reels:
        return {"account_url": account_url, "reels": [], "insights":
                {"error": "no reels found (account may be private or empty)"}}
    insights = aggregate_account_insights(account_url, reels,
                                          openai_api_key=openai_api_key)
    return {"account_url": account_url, "reels": reels, "insights": insights}
