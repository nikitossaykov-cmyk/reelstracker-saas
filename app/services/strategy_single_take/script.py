"""Single-take script autogen. Reuses the ruble-words helpers from
strategy_makeugc.script; the prompt targets one continuous ~30s take
(the v35/v36 narrative arc: заказала вслепую → что это аналог → цена →
реакция), optionally in ASMR whisper register."""
from __future__ import annotations

import json
import urllib.request

from app.services.strategy_makeugc.script import (
    SCRIPT_MODEL,
    _clean_brand,
    _format_rub,
)


def build_studio_script_prompt(
    *,
    product_name: str,
    brand: str,
    price_rub: float,
    dupe_price_rub: float,
    voice_style: str,
    cutaways: bool,
) -> str:
    tone = (
        "Регистр: интимный шёпот-ASMR, короткие фразы, паузы, как будто "
        "рассказывает секрет на ухо."
        if voice_style == "asmr"
        else "Регистр: живо и эмоционально, как рассказывает подруге."
    )
    if cutaways:
        actions = (
            "Часть 1 (до реакции) должна ЗАКАНЧИВАТЬСЯ ровно одной короткой "
            "фразой-обещанием действия («Сейчас открою…» или «Давайте "
            "попробуем…»), после которой идёт явная длинная пауза — в этот "
            "момент будет вставлен видеофрагмент с действием. Других "
            "обещаний действий («нанесу», «распылю», «покажу») быть не "
            "должно. Часть 2 — чистая реакция на запах, как уже "
            "случившееся впечатление.\n"
        )
    else:
        actions = (
            "В кадре только говорящая голова — героиня НЕ совершает действий. "
            "Запрещены обещания действий на камеру: «сейчас открою», «нанесу», "
            "«распылю», «покажу» и т.п. Про запах говори как про уже "
            "случившееся впечатление и ощущения.\n"
        )
    return (
        "Напиши сценарий озвучки для вертикального UGC-рилса, один "
        "непрерывный дубль на ~30 секунд (55-70 слов), на русском.\n"
        f"Продукт: аналог {_clean_brand(brand)} {product_name}.\n"
        f"Цена оригинала: {_format_rub(dupe_price_rub)}. "
        f"Цена аналога: {_format_rub(price_rub)} — цены произносить "
        "СЛОВАМИ, как написано.\n"
        "Арка: 1) заказала вслепую по чужой реакции, 2) что это аналог "
        "дорогого аромата, 3) цена-контраст, 4) живая реакция на запах.\n"
        f"{tone}\n"
        f"{actions}"
        "Без хэштегов, без эмодзи, без ремарок в скобках — только текст "
        "который будет произнесён."
    )


def generate_studio_script(
    *,
    product_name: str,
    brand: str,
    price_rub: float,
    dupe_price_rub: float,
    voice_style: str,
    cutaways: bool,
    openai_api_key: str,
) -> str:
    prompt = build_studio_script_prompt(
        product_name=product_name, brand=brand,
        price_rub=price_rub, dupe_price_rub=dupe_price_rub,
        voice_style=voice_style, cutaways=cutaways,
    )
    body = {
        "model": SCRIPT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.9,
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
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"].strip()
