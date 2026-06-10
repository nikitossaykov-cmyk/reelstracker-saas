"""
Composer — превращает ContentRecipe + RemakeParams в финальный
GenerationRequest для провайдера (Runway/Veo/Gemini Omni/Mock).

Главная работа: подставить плейсхолдеры из canonical_prompt:
  [PRODUCT], [FACE], [VOICE], [BRAND], [LOCATION], [PALETTE], [OUTFIT].
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RemakeParams:
    """Параметры подмены — кладёт юзер в POST /api/remakes."""
    brand: Optional[str] = None
    product_description: Optional[str] = None       # "luxury black perfume bottle, glossy"
    face_description: Optional[str] = None          # "young woman, mid-20s, strawberry-blonde"
    voice_description: Optional[str] = None         # "warm female voice, casual Russian"
    location_description: Optional[str] = None      # "minimalist marble bathroom"
    outfit_description: Optional[str] = None        # "beige trench, gold hoop earrings"
    palette: Optional[str] = None                   # "warm muted, soft window light"
    extra_instructions: Optional[str] = None        # "ensure no text in scene"
    init_image_url: Optional[str] = None            # PR #20: first frame of source for image-to-video


_PLACEHOLDER_RE = re.compile(r"\[(PRODUCT|FACE|VOICE|BRAND|LOCATION|PALETTE|OUTFIT|INSTRUCTIONS)\]")


def _value_for(token: str, params: RemakeParams) -> str:
    """Подставить значение или fallback-placeholder если не задано."""
    m = {
        "PRODUCT":     params.product_description or "the brand product",
        "FACE":        params.face_description or "the same person",
        "VOICE":       params.voice_description or "the original voice",
        "BRAND":       params.brand or "the brand",
        "LOCATION":    params.location_description or "the same location",
        "PALETTE":     params.palette or "the same color palette and lighting",
        "OUTFIT":      params.outfit_description or "the original outfit",
        "INSTRUCTIONS": params.extra_instructions or "",
    }
    return m.get(token, f"[{token}]")


def render_prompt(canonical_prompt: str, params: RemakeParams) -> str:
    """Подставить плейсхолдеры в canonical_prompt из recipe."""
    if not canonical_prompt:
        raise ValueError("recipe.canonical_prompt пуст — нечего рендерить")

    def repl(m: re.Match) -> str:
        return _value_for(m.group(1), params)

    rendered = _PLACEHOLDER_RE.sub(repl, canonical_prompt)
    # Если юзер задал extra_instructions и в prompt нет [INSTRUCTIONS] —
    # дописываем в конец.
    if params.extra_instructions and "[INSTRUCTIONS]" not in canonical_prompt:
        rendered += f"\n\nAdditional instructions: {params.extra_instructions}"
    return rendered.strip()


def build_prompt_from_recipe_dict(recipe_dict: dict, params: RemakeParams) -> str:
    """Если у recipe нет готового canonical_prompt — собрать его из полей."""
    parts: list[str] = []
    if name := recipe_dict.get("name"):
        parts.append(f"Format: {name}.")
    if hook := recipe_dict.get("hook"):
        if hook.get("text"):
            parts.append(f"Hook (first {hook.get('duration_sec', 3)}s): {hook['text']}")
        if hook.get("type"):
            parts.append(f"Hook type: {hook['type']}.")
    if motifs := recipe_dict.get("visual_motifs"):
        parts.append("Visual motifs: " + ", ".join(motifs) + ".")
    if structure := recipe_dict.get("structure"):
        parts.append("Structure:")
        for step in structure[:10]:
            sec = step.get("sec", "?")
            action = step.get("action", "")
            parts.append(f"  {sec}s — {action}")
    if cta := recipe_dict.get("cta"):
        parts.append(
            f"CTA: mention [BRAND] ({cta.get('brand_mention_count', 1)}× ),"
            f" tone {cta.get('tone', 'casual')}, "
            f"placement at ~{cta.get('placement_sec', '?')}s."
        )
    parts.append("Featuring [FACE] as the on-camera person and [PRODUCT] as the product.")
    parts.append("Setting: [LOCATION]. Outfit: [OUTFIT]. Palette: [PALETTE].")
    if params.extra_instructions:
        parts.append("Additional: [INSTRUCTIONS].")
    text = "\n".join(parts)
    return render_prompt(text, params)
