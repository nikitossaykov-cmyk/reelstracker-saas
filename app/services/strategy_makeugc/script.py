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


def _format_rub(amount: float) -> str:
    """Render rubles in the natural Russian way the persona would say
    out loud — not "44160 рублей" but "сорок четыре тысячи" / "тысячу".
    Keeps thousands/hundreds; rounds aggressively (people don't say
    'четыре тысячи триста двенадцать рублей' in a 24-sec reel).
    """
    if amount < 1000:
        return f"{int(round(amount))} рублей"
    if amount < 10_000:
        whole = int(round(amount / 100)) * 100
        return f"{whole} рублей"
    if amount < 100_000:
        whole = int(round(amount / 1000))
        return f"{whole} тысяч рублей"
    whole = int(round(amount / 1000))
    return f"{whole} тысяч рублей"


def _build_prompt(
    *,
    product_name: str,
    premium_brand: str,
    premium_price_rub: float,
    mimic_price_rub: float,
    persona_style: str,
) -> str:
    ratio = premium_price_rub / mimic_price_rub if mimic_price_rub else 0
    style_voice = {
        "average-girl": (
            "обычная девочка в комнате, очень живо, эмоционально, как "
            "рассказывает подруге что-то прикольное; короткие фразы, "
            "восклицания, искренний восторг"
        ),
        "glam-blonde": (
            "уверенная, заводная блогерка-обзорщица, темп быстрый, "
            "энергичные восклицания"
        ),
        "brunette-glasses": (
            "вдумчиво но живо, как-будто делится открытием; чуть "
            "медленнее, но всё равно эмоционально"
        ),
        "older-tester": (
            "взрослая, тёплая, эмоционально делится — но без скучного "
            "лекторского тона"
        ),
    }.get(persona_style, "обычная девочка, живо, эмоционально")

    premium_price_str = _format_rub(premium_price_rub)
    mimic_price_str = _format_rub(mimic_price_rub)

    return (
        "Напиши УСТНЫЙ скрипт для 24-секундного UGC-рилса про "
        "парфюм-дюп. Это говорящая голова — НЕ маркетинг-копирайтинг, "
        "не реклама. Девочка говорит подруге как с подругой.\n\n"
        f"Тон: {style_voice}. На русском.\n\n"
        f"Продукт: {product_name}\n"
        f"Дюп на: {premium_brand} (оригинал стоит {premium_price_str})\n"
        f"Наша цена: {mimic_price_str} "
        f"(в {ratio:.0f} раз дешевле)\n\n"
        "Структура (не пиши заголовки — только сам текст подряд, без "
        "разделителей):\n"
        "1) Хук-завлекалка БЕЗ слова «посмотрите»: что-нибудь типа "
        "«слушай», «знаешь что», «прикинь», «ну ты не поверишь» "
        "(2-3 сек)\n"
        f"2) Сравнение: «вместо {premium_price_str} за {premium_brand} "
        f"взяла за {mimic_price_str}». Дальше — впечатление от "
        "запаха простыми словами (как пахнет — сладко, тепло, свежо), "
        "8-10 сек\n"
        "3) Конкретика: с чем носить, когда, держится ли (5-6 сек)\n"
        "4) Призыв-вопрос: «А ты бы взяла такой за столько?» (2-3 сек)\n\n"
        "ЖЁСТКИЕ ЗАПРЕТЫ (модель не должна их генерировать):\n"
        "— НЕ «посмотрите», «посмотри-ка», «гляньте» — ElevenLabs ставит "
        "неправильное ударение. Используй «слушай», «прикинь», "
        "«знаешь что».\n"
        "— НЕ «древесные ноты», «древесность», «ниша», «парфюм», "
        "«флакон», «аромат», «парфюмерия», «окутывает», «обволакивает». "
        "Можно: «пахнет как», «такая штука», «такой запах», «бутылёк».\n"
        "— НЕ «идентичный», «копия», «оригинал», «дюп», «реплика». "
        "Можно: «вдохновлён», «похож», «как», «такой же».\n"
        "— НЕ «в этом видео», «подписывайся», «ссылка в описании».\n"
        "— НЕ «вау», «потрясающе», «невероятно», «представьте». "
        "Можно простые «ой», «прикинь», «слушай», «реально», «короче».\n"
        "— Обращайся на «ты», не «вы».\n"
        "— Цены ТОЛЬКО в рублях, ТОЛЬКО как написано выше "
        f"({premium_price_str}, {mimic_price_str}). НЕ долларах, НЕ "
        "цифрами «44160».\n"
        "— ~55-70 слов всего.\n"
        "— Живые короткие фразы, восклицания, эмоция.\n\n"
        "Выведи ТОЛЬКО текст скрипта одним блоком, без кавычек, без "
        "пояснений."
    )


def generate_script(
    *,
    product_name: str,
    premium_brand: str,
    premium_price_rub: float,
    mimic_price_rub: float,
    persona_style: str,
    openai_api_key: str,
) -> str:
    prompt = _build_prompt(
        product_name=product_name,
        premium_brand=premium_brand,
        premium_price_rub=premium_price_rub,
        mimic_price_rub=mimic_price_rub,
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
