"""Voiceover script generation.

The helper-bot R&D arrived at a fixed 30-sec reel formula:
  3s hook + 10s wide-shot + 3s cutaway + 8s wide-shot + 3s lips + 3s outro
24 seconds of active speech. At natural Russian pacing that's ~55-70
words. We generate a single contiguous script for the full talking-head
portion here; later stages (PR3+) slice it for lipsync and remix.

The prompt nudges gpt-4o-mini towards the helper-bot's tonal target:
honest-girl-on-her-bed, not commercial copywriter. That's what converts
on the makeugc.ai reference look (tested through 7 iterations on the
helper side, captured in handoff-2026-06-17.md §40-62).
"""
from __future__ import annotations

import json
import os
import urllib.request


SCRIPT_MODEL = "gpt-4o-mini"


def _build_prompt(
    *,
    product_name: str,
    premium_brand: str,
    premium_price_usd: float,
    mimic_price_usd: float,
    persona_style: str,
) -> str:
    ratio = premium_price_usd / mimic_price_usd if mimic_price_usd else 0
    style_voice = {
        "average-girl": "обычная девочка в комнате, удивлённая, искренняя, лёгкое волнение",
        "glam-blonde": "уверенная, тёплая, лёгкий понт, как блогерка-обзорщица",
        "brunette-glasses": "вдумчиво, спокойно, как-будто разбирает по полочкам",
        "older-tester": "взрослая, мягкая, делится опытом",
    }.get(persona_style, "обычная девочка в комнате, удивлённая, искренняя")

    return (
        "Напиши УСТНЫЙ скрипт для 24-секундного UGC-рилса про парфюм-дюп. "
        "Это говорящая голова — НЕ маркетинг-копирайтинг, не реклама. "
        f"Тон: {style_voice}. На русском.\n\n"
        f"Продукт: {product_name}\n"
        f"Дюп на: {premium_brand} (оригинал ${premium_price_usd:.0f})\n"
        f"Наша цена: ${mimic_price_usd:.0f} "
        f"(в {ratio:.0f} раз дешевле)\n\n"
        "Структура (не пиши заголовки — только сам текст подряд, без "
        "разделителей):\n"
        "1) Хук-шок: одна короткая фраза-удивление (2-3 секунды)\n"
        f"2) Сравнение: «вместо ${premium_price_usd:.0f} за {premium_brand} "
        f"взяла за ${mimic_price_usd:.0f}». Дальше — впечатление от запаха "
        "своими словами (8-10 сек)\n"
        "3) Конкретика: на что похоже, когда носить, как держится (5-6 сек)\n"
        "4) Призыв-вопрос в конце: «А ты бы взяла такой за столько же?» "
        "(2-3 сек)\n\n"
        "Запрет:\n"
        "— нельзя слова «идентичный», «копия», «оригинал»; можно «вдохновлён», "
        "«похож», «той же ноты»\n"
        "— никакого «в этом видео», «подписывайся», «ссылка в описании»\n"
        "— коротко, без воды, без слов «потрясающе», «невероятно»\n"
        "— ~55-70 слов всего\n\n"
        "Выведи ТОЛЬКО текст скрипта одним блоком, без кавычек, без "
        "пояснений."
    )


def generate_script(
    *,
    product_name: str,
    premium_brand: str,
    premium_price_usd: float,
    mimic_price_usd: float,
    persona_style: str,
    openai_api_key: str,
) -> str:
    prompt = _build_prompt(
        product_name=product_name,
        premium_brand=premium_brand,
        premium_price_usd=premium_price_usd,
        mimic_price_usd=mimic_price_usd,
        persona_style=persona_style,
    )
    body = {
        "model": SCRIPT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8,
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {openai_api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(
            f"OpenAI script-gen HTTP {e.code}: {body_text}"
        ) from e

    text = data["choices"][0]["message"]["content"].strip()
    text = text.strip("\"'«»")
    if not text:
        raise RuntimeError("OpenAI returned empty script")
    return text


def resolve_openai_key(user_key: str | None) -> str | None:
    """Per-user key wins; falls back to shared OPENAI_API_KEY env."""
    return user_key or os.getenv("OPENAI_API_KEY")
