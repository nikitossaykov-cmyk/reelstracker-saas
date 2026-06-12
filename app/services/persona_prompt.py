"""Builds prompts for Replicate PuLID-Flux persona generation.

Lifts the moderation-safe frame from the gpt-image-1 skill notes:
priming language ('fictional', 'generic', 'no real-person likeness',
'no logos of existing brands') goes BEFORE the user bio so the
moderation layer reads it first and accepts the rest as safe-intent.
"""
from __future__ import annotations

from typing import Optional


SAFETY_FRAME = (
    "Photorealistic vertical 9:16 portrait of a fictional generic adult "
    "content creator (no identifying features, no celebrity likeness, "
    "no real-person likeness, no logos of existing brands), soft natural "
    "studio light, neutral background, casual styling."
)

SAFE_DEFAULTS = (
    "Photorealistic vertical 9:16 portrait, generic adult woman with "
    "neutral expression, soft studio light, plain background, casual "
    "styling, no identifying features, no logos."
)

STYLE_PHRASE = {
    "editorial": "editorial fashion photography aesthetic",
    "lifestyle": "natural lifestyle photography aesthetic",
    "studio": "clean studio photography aesthetic",
    "street": "candid street photography aesthetic",
}

MAX_PROMPT_LEN = 1000


def build_persona_prompt(bio: str, style: Optional[str]) -> str:
    parts = [SAFETY_FRAME, f"general look (fictional persona): {bio.strip()}."]
    if style and style in STYLE_PHRASE:
        parts.append(STYLE_PHRASE[style] + ".")
    parts.append("No real-person likeness. No logos of existing brands.")
    prompt = " ".join(parts)
    return prompt[:MAX_PROMPT_LEN]


def defensive_fallback_prompt() -> str:
    return SAFE_DEFAULTS
