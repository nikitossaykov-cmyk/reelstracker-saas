"""
Recipe Extractor — превращает Analyzer-output (transcript + visual_summary
+ scenes + hook_type) в детальный ContentRecipe через GPT-4o-mini.

v2 (PR #19): больше токенов, гораздо более конкретный system prompt,
extended schema — shot_list по секундам, точные тексты overlay'ев,
brand convention (UGC/studio), specific product/face/location detail.

Стоимость: ~$0.003 per recipe (3x prev из-за бОльших tokens).
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
"recipe" that another AI model will use to RECREATE this video as closely
as possible — same shots, same pacing, same on-screen text, same vibe.

CRITICAL RULES:
1. BE SPECIFIC, NOT GENERIC. Bad: "distressed woman". Good: "young woman
   ~25, blonde wavy hair, wearing yellow tank top, lying on white pillow,
   tears on cheeks, hand at mouth".
2. COPY EXACT TEXT from transcript and any on-screen overlays — verbatim,
   in source language (Russian if source is Russian).
3. NOTE THE PRODUCTION STYLE. Is it UGC handheld phone with harsh flash?
   Studio-shot with soft window light? Selfie? Tripod? Note camera height,
   lens type if guessable, color grade.
4. NOTE COMPOSITE ELEMENTS. If video has screenshots of messages /
   social media posts inserted, transcribe their content.
5. NOTE THE PRODUCT EXACTLY. If you can read a label ("DARK OPIUM") —
   say so. If it's a perfume bottle — describe shape, color, label.

Output STRICT JSON only — no markdown fences, no commentary. Schema:

{
  "name": "short snappy title in source language (≤80 chars)",
  "hook_type": "POV|REACTION|DUPE|ASMR_UNBOXING|TWIST|AESTHETIC_TAG|COMPLIMENT_MONSTER|AUTHORITY_REVIEW|TUTORIAL|OTHER",
  "duration_sec": integer,
  "language": "ru|en|...",
  "production_style": {
    "camera": "handheld phone selfie|tripod static|gimbal|professional camera",
    "lighting": "harsh flash from above|soft window light|studio softbox|...",
    "color_grade": "warm muted|cool harsh|natural|...",
    "ugc_authenticity": "high - mimics real UGC|medium|low - clearly produced",
    "format": "front-facing single shot|montage of shots|composite with screenshots|talking head|..."
  },
  "characters": [
    {"role": "protagonist|antagonist|product",
     "physical": "EXACT description — age, hair color/length/style, eye color if visible, body type, ethnicity, skin tone, distinctive features",
     "outfit": "EXACT clothing — color, type, brand if visible, accessories",
     "emotion_arc": "starts X → middle Y → ends Z"}
  ],
  "shot_list": [
    {"sec": 0, "duration_sec": 3,
     "shot_type": "close-up|medium|wide|insert|screenshot",
     "subject": "what's in frame — be specific",
     "action": "what happens in this shot, exact movements",
     "voice_or_audio": "what's said verbatim, OR description of music/sound",
     "overlay_text": "exact text shown on-screen verbatim (Russian if Russian)",
     "overlay_position": "top|bottom|center",
     "transition_to_next": "hard cut|fade|swipe|none"},
    ...one entry per shot...
  ],
  "product_details": {
    "what": "the product being shown",
    "brand_name_visible": "exact text on label if readable, else null",
    "physical_appearance": "exact bottle/packaging description",
    "appears_at_sec": [list of seconds when product is shown],
    "treatment": "hero shot|insert|incidental|prominent"
  },
  "audio_strategy": {
    "type": "original_dialogue|trending_sound|voiceover_only|ambient_no_music|silent_with_overlays",
    "voice_description": "EXACT tone — gender, age, accent, mood, language",
    "music_description": "if music present — genre, mood, tempo",
    "sound_id": "if known trending sound name, else null"
  },
  "cta": {
    "brand_mention_count": integer,
    "marketplace": "wildberries|ozon|other|null",
    "tone": "casual conversational|hard sell|expert review|emotional",
    "placement_sec": "when and where the CTA happens — be specific",
    "exact_text": "EXACT words of the CTA if any"
  },
  "canonical_prompt": "ONE LONG PARAGRAPH (~500-800 words) that recreates this video as faithfully as possible. Write it as a Veo/Runway prompt. Include EVERY detail above. Use [BRAND] and [PRODUCT] as substitution tokens for the brand/product (so the user can swap them). KEEP everything else (composition, text overlays — in source language, character description, lighting, style) intact."
}

If a field is unknown, use null. Goal: a downstream AI should be able to
recreate a near-identical video, only swapping [BRAND] and [PRODUCT].
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
    timeout: int = 120,
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
        parts.append(f"TRANSCRIPT (exact words, verbatim):\n{transcript[:4000]}")
    if visual_summary:
        parts.append(f"VISUAL SUMMARY:\n{visual_summary[:3000]}")
    if scenes:
        scenes_compact = [
            f"  - shot {i+1}: {s['start']:.1f}-{s['end']:.1f}s ({s['duration']:.1f}s)"
            for i, s in enumerate(scenes[:30])
        ]
        parts.append("SHOT BOUNDARIES (from scene detection):\n" + "\n".join(scenes_compact))
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
            max_tokens=6000,  # v2: 3x — нужно для detailed recipes
            temperature=0.2,  # ниже temperature → более точное копирование
        )
    except Exception as e:
        raise RecipeExtractError(f"OpenAI {type(e).__name__}: {str(e)[:300]}")

    raw = (resp.choices[0].message.content or "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RecipeExtractError(f"LLM вернул не-JSON: {e}; raw={raw[:300]}")

    logger.info(f"extracted recipe v2 '{parsed.get('name', '?')}' "
                f"hook={parsed.get('hook_type')} "
                f"shots={len(parsed.get('shot_list') or [])} "
                f"chars={len(parsed.get('canonical_prompt') or '')}")
    return parsed, raw
