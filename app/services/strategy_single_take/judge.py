"""Gemini video QC — port of /opt/tg-bot/tools/reel_judge.py.

Non-blocking by contract: the worker catches JudgeError and still marks
the job READY (badge «QC недоступен»). Free-tier flash 429s routinely;
flash-lite is the fallback that actually answers.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests


BASE = "https://generativelanguage.googleapis.com"
JUDGE_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
RETRYABLE = {429, 500, 503}

RUBRIC_PROMPT = """\
Ты — строгий судья качества коротких вертикальных видео (Instagram Reels) для AI-UGC пайплайна.
Оцени видео по рубрике. Не льсти: 5 — это «средний живой UGC», 8+ — только если реально сильно.

Верни СТРОГО JSON:
{
  "scores": {
    "hook": 0-10,
    "visual_quality": 0-10,
    "text_readability": 0-10,
    "lipsync": 0-10,
    "audio": 0-10,
    "pacing": 0-10,
    "authenticity": 0-10
  },
  "overall": 0-10,
  "verdict": "pass" | "fix" | "reject",
  "top_issues": ["конкретная проблема с таймкодом"],
  "timeline_notes": ["0:00-0:02 ...", "..."]
}

Особое внимание: читаемость мелкого текста на этикетках продукта,
переходы между склейками, синхрон губ. Все замечания — с таймкодами.
"""


class JudgeError(RuntimeError):
    pass


def upload_video(path: Path, key: str) -> str:
    """Resumable upload to Gemini Files API → file_uri (waits for ACTIVE)."""
    size = path.stat().st_size
    start = requests.post(
        f"{BASE}/upload/v1beta/files",
        params={"key": key},
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": "video/mp4",
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": path.name}},
        timeout=30,
    )
    start.raise_for_status()
    upload_url = start.headers["X-Goog-Upload-URL"]

    with open(path, "rb") as f:
        up = requests.post(
            upload_url,
            headers={
                "X-Goog-Upload-Command": "upload, finalize",
                "X-Goog-Upload-Offset": "0",
                "Content-Length": str(size),
            },
            data=f,
            timeout=300,
        )
    up.raise_for_status()
    info = up.json()["file"]

    name = info["name"]
    for _ in range(60):
        if info.get("state") == "ACTIVE":
            return info["uri"]
        if info.get("state") == "FAILED":
            raise JudgeError(f"Gemini не смог обработать видео: {info}")
        time.sleep(2)
        info = requests.get(
            f"{BASE}/v1beta/{name}", params={"key": key}, timeout=30,
        ).json()
    raise JudgeError("Таймаут: видео не стало ACTIVE за 2 минуты")


def judge_uploaded(file_uri: str, *, key: str, brief: str | None) -> dict:
    """Rotate through JUDGE_MODELS on quota/transient codes."""
    prompt = RUBRIC_PROMPT
    if brief:
        prompt += f"\nКонтекст от автора (что задумывалось): {brief}\n"
    body = {
        "contents": [{
            "parts": [
                {"file_data": {"file_uri": file_uri, "mime_type": "video/mp4"}},
                {"text": prompt},
            ],
        }],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.2,
        },
    }
    last = ""
    for model in JUDGE_MODELS:
        r = requests.post(
            f"{BASE}/v1beta/models/{model}:generateContent",
            params={"key": key}, json=body, timeout=300,
        )
        if r.status_code == 200:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        last = f"{model}: HTTP {r.status_code}"
        if r.status_code not in RETRYABLE:
            raise JudgeError(f"judge hard fail — {last}: {r.text[:300]}")
    raise JudgeError(f"все judge-модели исчерпаны — {last}")


def judge_video(path: Path, *, api_key: str, brief: str | None = None) -> dict:
    uri = upload_video(path, api_key)
    return judge_uploaded(uri, key=api_key, brief=brief)
