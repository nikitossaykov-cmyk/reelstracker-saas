"""
Recipe Extractor — превращает Analyzer-output (transcript + visual_summary
+ scenes + hook_type) в структурированный ContentRecipe через one-shot
GPT-4o-mini call с JSON schema.

Принципиально использует cheap LLM (gpt-4o-mini) — одна короткая
запись данных, нужна не глубокая reasoning. ~$0.001 за recipe.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RecipeExtractError(Exception):
    pass


SYSTEM_PROMPT = """\
You convert a short-form video (TikTok/Reels) analysis into a structured
"recipe" — a reusable template that another model can use to generate
new videos in the same format with different products/people/voices.

Output STRICT JSON only — no markdown fences, no commentary. Schema:

{
  "name": "short snappy title in source language (≤60 chars)",
  "hook_type": "POV|REACTION|DUPE|ASMR_UNBOXING|TWIST|AESTHETIC_TAG|COMPLIMENT_MONSTER|AUTHORITY_REVIEW|TUTORIAL|OTHER",
  "duration_sec": integer,
  "language": "ru|en|...|null",
  "hook": {
    "text": "first line said/shown to grab attention",
    "type": "what kind of hook (Q, shock, contrast, intrigue, ...)",
    "duration_sec": integer (typically 1-5),
    "delivery": "voiceover|on_screen_text|both|natural_dialogue"
  },
  "structure": [
    {"sec": float, "action": "what happens", "voice": "what's said", "overlay": "on-screen text"},
    ...one entry per scene...
  ],
  "visual_motifs": ["close-up bottle on marble", "soft window light", "9:16 vertical", ...],
  "audio_strategy": {
    "type": "original_dialogue|trending_sound|voiceover_only|ambient_no_music",
    "voice_description": "tone/gender if voiceover, null otherwise",
    "sound_id": "if known trending sound name, else null"
  },
  "cta": {
    "brand_mention_count": integer,
    "marketplace": "wildberries|ozon|null",
    "tone": "casual|hard_sell|expert",
    "placement_sec": float (when in video)
  },
  "canonical_prompt": "single paragraph (~300 words) that a Veo/Runway/Gemini-Omni model can ingest as a prompt to recreate the format with [PLACEHOLDER] tokens where the product/face/voice should be substituted"
}

If a field is unknown, use null. Be specific in visual_motifs — they become
the building blocks for the remake.
"""


def extract_recipe(
    *,
    transcript: Optional[str],
    visual_summary: Optional[str],
    scenes: Optional[list[dict]],
    hook_type: Optional[str],
    duration_sec: Optional[float],
    openai_api_key: str,
    model: str = "gpt-4o-mini",
    timeout: int = 60,
) -> tuple[dict[str, Any], str]:
    """Запросить рецепт у LLM. Возвращает (parsed_dict, raw_response_text)."""
    if not openai_api_key:
        raise RecipeExtractError("openai_api_key пуст")
    if not (transcript or visual_summary):
        raise RecipeExtractError(
            "Нечем извлекать: нет ни transcript, ни visual_summary "
            "(сначала запусти Analyzer)"
        )

    try:
        from openai import OpenAI
    except ImportError as e:
        raise RecipeExtractError(f"openai SDK не установлен: {e}")

    parts = []
    if transcript:
        parts.append(f"TRANSCRIPT:\n{transcript[:3000]}")
    if visual_summary:
        parts.append(f"VISUAL SUMMARY:\n{visual_summary[:2000]}")
    if scenes:
        scenes_compact = [
            f"  - {s['start']:.1f}–{s['end']:.1f}s ({s['duration']:.1f}s)"
            for s in scenes[:20]
        ]
        parts.append("SCENES:\n" + "\n".join(scenes_compact))
    if hook_type:
        parts.append(f"PRE-CLASSIFIED HOOK: {hook_type}")
    if duration_sec:
        parts.append(f"TOTAL DURATION: {duration_sec:.1f}s")

    user_msg = "\n\n".join(parts)
    client = OpenAI(api_key=openai_api_key, timeout=timeout)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            max_tokens=2000,
            temperature=0.3,
        )
    except Exception as e:
        raise RecipeExtractError(f"OpenAI {type(e).__name__}: {str(e)[:300]}")

    raw = (resp.choices[0].message.content or "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RecipeExtractError(f"LLM вернул не-JSON: {e}; raw={raw[:300]}")

    logger.info(f"extracted recipe '{parsed.get('name', '?')}' "
                f"hook={parsed.get('hook_type')} "
                f"structure_steps={len(parsed.get('structure') or [])}")
    return parsed, raw
