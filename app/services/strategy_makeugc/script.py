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


_UNITS_WORDS = {
    1: "один", 2: "два", 3: "три", 4: "четыре", 5: "пять",
    6: "шесть", 7: "семь", 8: "восемь", 9: "девять", 10: "десять",
    11: "одиннадцать", 12: "двенадцать", 13: "тринадцать",
    14: "четырнадцать", 15: "пятнадцать", 16: "шестнадцать",
    17: "семнадцать", 18: "восемнадцать", 19: "девятнадцать",
}
_TENS_WORDS = {
    20: "двадцать", 30: "тридцать", 40: "сорок", 50: "пятьдесят",
    60: "шестьдесят", 70: "семьдесят", 80: "восемьдесят", 90: "девяносто",
}
_HUNDREDS_WORDS = {
    100: "сто", 200: "двести", 300: "триста", 400: "четыреста",
    500: "пятьсот", 600: "шестьсот", 700: "семьсот", 800: "восемьсот",
    900: "девятьсот",
}


def _int_in_words(n: int) -> str:
    if n <= 0:
        return "ноль"
    if n in _UNITS_WORDS:
        return _UNITS_WORDS[n]
    if n in _TENS_WORDS:
        return _TENS_WORDS[n]
    if n in _HUNDREDS_WORDS:
        return _HUNDREDS_WORDS[n]
    if n < 100:
        # 21..99 (excluding 30/40/50/...)
        tens = (n // 10) * 10
        units = n % 10
        return f"{_TENS_WORDS[tens]} {_UNITS_WORDS[units]}"
    if n < 1000:
        # 101..999
        hund = (n // 100) * 100
        rest = n % 100
        return f"{_HUNDREDS_WORDS[hund]} {_int_in_words(rest)}".strip()
    # Fallback: leave as digits if outside what the persona would say
    # at conversational speed (rare for the prices Nick targets).
    return str(n)


def _thousands_suffix(n: int) -> str:
    """Russian "тысяча/тысячи/тысяч" pluralisation."""
    last_two = n % 100
    last = n % 10
    if 11 <= last_two <= 14:
        return "тысяч"
    if last == 1:
        return "тысяча"
    if 2 <= last <= 4:
        return "тысячи"
    return "тысяч"


def _format_rub(amount: float) -> str:
    """Render rubles ENTIRELY IN WORDS so ElevenLabs reads them
    distinctly. Numeric "50" was getting mumbled into "пьдесят"; spell
    "пятьдесят тысяч рублей" out and the model reads it cleanly.
    """
    n = int(round(amount))
    if n < 1000:
        return f"{_int_in_words(n)} рублей"
    thousands = n // 1000
    remainder = n % 1000

    if thousands == 1 and remainder == 0:
        return "тысяча рублей"
    thousands_word = _int_in_words(thousands)
    # Russian quirk: "одна тысяча", "две тысячи" — adjust the unit for
    # 1/2 when used as a numerative ("один" -> "одна", "два" -> "две").
    if thousands % 10 == 1 and thousands % 100 != 11:
        thousands_word = thousands_word.replace("один", "одна") \
            if thousands_word.endswith("один") else thousands_word
    elif thousands % 10 == 2 and thousands % 100 != 12:
        thousands_word = thousands_word.replace("два", "две") \
            if thousands_word.endswith("два") else thousands_word

    base = f"{thousands_word} {_thousands_suffix(thousands)}"
    if remainder == 0:
        return f"{base} рублей"
    return f"{base} {_int_in_words(remainder)} рублей"


def _clean_brand(brand: str) -> str:
    """Strip numeric model suffixes from brand names. "Baccarat Rouge
    540" → "Baccarat Rouge"; the persona transliterates to "Бакара руж"
    at script-gen time and the digit suffix would otherwise read as
    "пятьсот сорок" which sounds like product catalog talk.
    """
    parts = brand.strip().split()
    cleaned = [p for p in parts if not any(ch.isdigit() for ch in p)]
    return " ".join(cleaned).strip() or brand.strip()


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
    brand_clean = _clean_brand(premium_brand)

    return (
        "Напиши УСТНЫЙ скрипт для 24-секундного UGC-рилса про "
        "парфюм-дюп. Это говорящая голова — НЕ маркетинг-копирайтинг, "
        "не реклама. Девочка говорит подруге как с подругой.\n\n"
        f"Тон: {style_voice}. На русском.\n\n"
        f"Продукт: {product_name}\n"
        f"Дюп на: {brand_clean} (оригинал стоит {premium_price_str})\n"
        f"Наша цена: {mimic_price_str} "
        f"(в {ratio:.0f} раз дешевле)\n\n"
        "Структура (не пиши заголовки — только сам текст подряд, без "
        "разделителей):\n"
        "1) Хук-завлекалка БЕЗ слова «посмотрите»: что-нибудь типа "
        "«слушай», «знаешь что», «прикинь», «ну ты не поверишь» "
        "(2-3 сек)\n"
        f"2) Сравнение: «вместо {premium_price_str} за {brand_clean} "
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
        "— Цены ТОЛЬКО в рублях, ТОЛЬКО ПРОПИСЬЮ как написано выше "
        f"({premium_price_str}, {mimic_price_str}). НЕ долларах, НЕ "
        "цифрами «50000» или «50 тысяч» — только полные слова.\n"
        "— Бренд произноси по-русски как звучит: "
        f"«{brand_clean}» → транслитерируй («Baccarat Rouge» → "
        "«Бакара руж», «Tom Ford» → «Том Форд»). Номера моделей "
        "(540, 31 и т.п.) НЕ читай — они уже убраны.\n"
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
