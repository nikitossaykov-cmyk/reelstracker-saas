"""
Scene classifier — для каждого сегмента из ffmpeg scene-detect определяет
тип через GPT-4o-mini Vision (один кадр из середины сцены).

Типы:
  talking_head_live   — живая съёмка персонажа (лицо/тело в кадре)
  screenshot_static   — скрин Telegram/WhatsApp/IG/любой UI
  product_insert      — кадр с продуктом (бутылка/упаковка)
  text_card           — пустой фон с большим overlay-текстом
  b_roll              — пейзаж/runtime/общий план без сюжета

Reuse strategies для hybrid composer:
  regenerate          — Runway image_to_video (нужна новая generation)
  keep_original       — берём кусок исходника 1в1 (без затрат)
  text_template       — пересобираем через ffmpeg drawtext
  image_edit_overlay  — frame → GPT-image edit → static video (опц.)

Cost: ~$0.001 per scene (1 frame + 1 short classification call).
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


VALID_TYPES = {
    "talking_head_live", "screenshot_static", "product_insert",
    "text_card", "b_roll", "unknown",
}

TYPE_TO_STRATEGY = {
    "talking_head_live": "regenerate",
    "screenshot_static": "keep_original",
    "product_insert":    "image_edit_overlay",  # пока fallback на keep_original в worker
    "text_card":         "text_template",
    "b_roll":            "keep_original",
    # When the classifier hedges, do NOT spend Runway $ — keep the original
    # segment. Risk of leaving a non-remade clip is lower than risk of
    # mis-regenerating a screenshot/static text + drifting subtitles.
    "unknown":           "keep_original",
}

# If talking_head_live comes back below this threshold we treat it as
# "unknown" (→ keep_original). Empirically the classifier was over-firing
# talking_head_live on screenshots that had a person's avatar in the corner.
TALKING_HEAD_CONFIDENCE_MIN = 0.6


class SceneClassifyError(Exception):
    pass


SYSTEM_PROMPT = """\
Classify this single frame from a short-form video into ONE of these types.
Be CONSERVATIVE — when in doubt return "unknown" with low confidence.
Misclassifying a screenshot as talking_head_live wastes Runway budget AND
desyncs the burned-in subtitles, so the cost of false-positive
talking_head_live is high.

- talking_head_live: a real person CLEARLY dominates the frame (face/body
  takes ≥40% of canvas) AND looks like live footage (not a static photo
  or thumbnail). A small avatar in a corner does NOT count.
- screenshot_static: a screenshot of a chat (Telegram/WhatsApp/iMessage),
  social-media post, browser, app UI, or any rectangular text-heavy panel
  even if it is overlaid on another image.
- product_insert: close-up of a physical product (bottle, package, item
  in hand or on surface) with NO person dominating the frame.
- text_card: mostly plain/blurred background + LARGE on-screen text
  overlay (intro card, callout, slogan, big stat).
- b_roll: landscape, ambient/establishing shot, generic objects, no
  human or product focal subject.
- unknown: ambiguous or doesn't fit any category. USE THIS LIBERALLY —
  unknown is safe (we keep the original clip); a wrong talking_head_live
  is expensive.

Return JSON ONLY:
{"type": "...", "confidence": 0.0-1.0,
 "visible_text": "exact text if any, else null",
 "description": "≤120 chars what's in frame"}

Confidence calibration:
- 0.9+ : obvious, no other category plausible
- 0.7-0.9 : likely but a second category is plausible
- 0.5-0.7 : best guess, would not bet money
- <0.5  : prefer to set type=unknown
"""


def _grab_frame_b64(video_path: Path, time_sec: float) -> Optional[str]:
    out = video_path.parent / f"frame_{time_sec:.1f}.jpg"
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", str(time_sec), "-i", str(video_path),
         "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "3", str(out)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if r.returncode != 0 or not out.exists():
        return None
    data = out.read_bytes()
    try: out.unlink()
    except OSError: pass
    return base64.b64encode(data).decode()


def classify_scene(
    video_path: Path,
    start_sec: float,
    end_sec: float,
    openai_api_key: str,
    model: str = "gpt-4o-mini",
    timeout: int = 30,
) -> dict:
    """Один scene → классификация. Возвращает dict с type/strategy/visible_text."""
    if not openai_api_key:
        raise SceneClassifyError("openai_api_key пуст")

    mid = (start_sec + end_sec) / 2
    b64 = _grab_frame_b64(video_path, mid)
    if not b64:
        return {"type": "unknown", "strategy": "regenerate", "visible_text": None,
                "description": "frame extraction failed", "start": start_sec, "end": end_sec}

    try:
        from openai import OpenAI
    except ImportError as e:
        raise SceneClassifyError(f"openai SDK не установлен: {e}")
    client = OpenAI(api_key=openai_api_key, timeout=timeout)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": f"Frame at {mid:.1f}s of a {end_sec-start_sec:.1f}s shot."},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"}},
                ]},
            ],
            response_format={"type": "json_object"},
            max_tokens=300,
            temperature=0,
        )
    except Exception as e:
        raise SceneClassifyError(f"OpenAI {type(e).__name__}: {str(e)[:200]}")

    raw = (resp.choices[0].message.content or "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"type": "unknown", "visible_text": None, "description": raw[:120]}
    t = parsed.get("type", "unknown")
    if t not in VALID_TYPES:
        t = "unknown"
    try:
        conf = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0

    # Downgrade low-confidence talking_head_live → unknown (= keep_original).
    # The frame was probably a screenshot with a small avatar; regenerating
    # would burn ~$0.25 Runway and drift the subtitles.
    if t == "talking_head_live" and conf < TALKING_HEAD_CONFIDENCE_MIN:
        logger.info(
            f"  scene @ {start_sec:.1f}s: talking_head_live conf={conf:.2f} "
            f"< {TALKING_HEAD_CONFIDENCE_MIN} → downgraded to unknown"
        )
        t = "unknown"

    return {
        "start": start_sec,
        "end": end_sec,
        "duration": round(end_sec - start_sec, 3),
        "type": t,
        "strategy": TYPE_TO_STRATEGY[t],
        "visible_text": parsed.get("visible_text"),
        "description": (parsed.get("description") or "")[:200],
        "confidence": conf,
    }


def classify_scenes(
    video_path: Path,
    scenes: list[dict],
    openai_api_key: str,
) -> list[dict]:
    """Классификация всех сцен. Прогресс — последовательно (можно сделать
    параллельно через ThreadPoolExecutor для скорости — todo если >20 сцен)."""
    out = []
    for i, s in enumerate(scenes):
        try:
            enriched = classify_scene(video_path, s["start"], s["end"], openai_api_key)
            logger.info(f"  scene {i+1}/{len(scenes)} @ {s['start']:.1f}s: "
                        f"{enriched['type']} → {enriched['strategy']}")
        except SceneClassifyError as e:
            logger.warning(f"  scene {i+1} classify failed: {e}")
            enriched = {**s, "type": "unknown", "strategy": "keep_original",
                        "visible_text": None, "description": str(e)[:120]}
        out.append(enriched)
    return out
