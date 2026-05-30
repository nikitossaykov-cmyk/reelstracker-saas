"""
Классификатор типа хука рилса по transcript + visual_summary.

Возвращает один из enum-значений (см. HOOK_TYPES). Использует GPT-4o-mini
как cheap LLM. Если ни одно значение не подходит — возвращает "OTHER".
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ClassifyError(Exception):
    pass


# Должно соответствовать тому, что мы потом используем в Recipe Extractor.
# Канонические форматы PerfumeTok из mimic_content_pack_2026_05.
HOOK_TYPES = [
    "POV",                 # POV: ты на свидании / ты впервые...
    "REACTION",            # Stranger asks about your scent / реакция кого-то
    "DUPE",                # X за 1500₽ vs Y за 30000₽
    "ASMR_UNBOXING",       # close-up распаковка + first sniff
    "TWIST",               # hook → reveal → twist (drama)
    "AESTHETIC_TAG",       # Clean Girl / Mob Wife / Old Money — определи себя
    "COMPLIMENT_MONSTER",  # Top-N compliment pullers
    "AUTHORITY_REVIEW",    # «я перепробовала 30 ароматов» — экспертиза
    "TUTORIAL",            # как / лайфхак
    "OTHER",
]


SYSTEM_PROMPT = (
    "You classify TikTok/Reels videos into one of these hook formats:\n"
    + "\n".join(f"- {h}" for h in HOOK_TYPES)
    + "\n\nReturn ONLY the single best-matching label in CAPS, no explanation. "
    "If unclear → OTHER."
)


def classify_hook(
    transcript: Optional[str],
    visual_summary: Optional[str],
    openai_api_key: str,
    model: str = "gpt-4o-mini",
    timeout: int = 30,
) -> str:
    """Классифицировать рилс по тексту + визуалу.

    Если оба источника пустые, вернуть OTHER без вызова API.
    """
    if not (transcript or visual_summary):
        return "OTHER"
    if not openai_api_key:
        raise ClassifyError("openai_api_key пуст — нечем авторизоваться")

    try:
        from openai import OpenAI
    except ImportError as e:
        raise ClassifyError(f"openai SDK не установлен: {e}")

    parts = []
    if transcript:
        parts.append(f"TRANSCRIPT:\n{transcript[:2000]}")
    if visual_summary:
        parts.append(f"VISUAL SUMMARY:\n{visual_summary[:1500]}")
    user_msg = "\n\n".join(parts)

    client = OpenAI(api_key=openai_api_key, timeout=timeout)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=20,
            temperature=0,
        )
    except Exception as e:
        raise ClassifyError(f"OpenAI {type(e).__name__}: {str(e)[:200]}")

    raw = (resp.choices[0].message.content or "").strip().upper()
    # LLM может вернуть с пробелами, точками, или объяснением
    for label in HOOK_TYPES:
        if label in raw:
            logger.info(f"hook classified as {label}")
            return label
    logger.warning(f"hook classifier returned unrecognized: {raw!r}")
    return "OTHER"
