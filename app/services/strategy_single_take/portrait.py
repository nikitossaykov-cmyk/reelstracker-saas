"""Studio portrait — google/nano-banana-pro on Replicate.

The real product photo goes in as image_input so the label survives;
the prompt carries an explicit misspelling ban-list (nano-banana
otherwise invents plausible-looking gibberish in fine print). Rule
from vault v11 iterations: never run more than 2 label-edit passes —
the 3rd degrades fine print. POC does a single pass.
"""
from __future__ import annotations

import base64

from app.services.replicate_client import ReplicateClient
from app.services.strategy_makeugc.portrait import _extract_output


MODEL_NANO = "google/nano-banana-pro"
COST_PER_IMAGE_USD = 0.15

_FRAMING_ASMR = (
    "Extreme close-up vertical portrait: an average European girl, "
    "23-27, leans toward a large studio condenser microphone with a pop "
    "filter, whisper-review ASMR setting, dim cozy bedroom light. She "
    "holds the product bottle right next to her face so the label faces "
    "the camera."
)
_FRAMING_NORMAL = (
    "Vertical UGC selfie portrait: an average European girl, 23-27, "
    "plain face, natural skin, sitting in her bedroom with soft window "
    "light, holding the product bottle in one hand near her face, label "
    "facing the camera, genuine slight smile."
)


def build_studio_prompt(*, product_name: str, brand: str, asmr: bool) -> str:
    framing = _FRAMING_ASMR if asmr else _FRAMING_NORMAL
    return (
        f"{framing} The bottle is the exact product from the reference "
        f"photo — keep its shape, cap, color and label identical. The "
        f"label text must read exactly «{brand}» and «{product_name}». "
        f"Do not misspell, redraw, translate or invent ANY text on the "
        f"label or anywhere in frame; if a word is not clearly legible "
        f"in the reference photo, keep it blurred rather than guessing. "
        f"Photorealistic, shot on a phone, shallow depth of field, "
        f"9:16 composition."
    )


def build_persona_prompt(
    *, product_name: str, brand: str, asmr: bool, look_prompt: str | None,
) -> str:
    """Identity-референс: портрет персоны первым image_input, продукт вторым.
    Без look_prompt образ фиксируется как в референсе (анти-дрифт);
    с look_prompt — то же лицо, новый образ."""
    if look_prompt:
        look = (
            f"Change her look: {look_prompt}. Everything else about her "
            f"face must stay identical to the first reference image."
        )
    else:
        look = (
            "Keep her hairstyle, outfit, room and lighting exactly as in "
            "the first reference image."
        )
    setting = (
        "She leans toward a large studio condenser microphone with a pop "
        "filter, whisper-review ASMR setting, dim cozy light. "
        if asmr else ""
    )
    return (
        "Photorealistic vertical UGC portrait of the SAME woman as in the "
        "first reference image — identical face, identical hairstyle, "
        f"natural skin. {look} {setting}"
        "She holds the EXACT product bottle from the second reference "
        "image in one hand near her face, label facing the camera — keep "
        "its shape, cap, color and label identical. The label text must "
        f"read exactly «{brand}» and «{product_name}». Do not misspell, "
        "redraw, translate or invent ANY text on the label or anywhere in "
        "frame; if a word is not clearly legible in the reference photo, "
        "keep it blurred rather than guessing. Photorealistic, shot on a "
        "phone, shallow depth of field, 9:16 composition."
    )


def generate_persona_portrait(
    *,
    persona_bytes: bytes,
    product_image_bytes: bytes,
    product_content_type: str,
    product_name: str,
    brand: str,
    asmr: bool,
    look_prompt: str | None,
    replicate_api_key: str,
) -> tuple[bytes | str, float]:
    """nano-banana-pro: persona ref first, product ref second."""
    persona_uri = (
        f"data:image/jpeg;base64,{base64.b64encode(persona_bytes).decode()}"
    )
    product_uri = (
        f"data:{product_content_type};base64,"
        f"{base64.b64encode(product_image_bytes).decode()}"
    )
    params = {
        "prompt": build_persona_prompt(
            product_name=product_name, brand=brand,
            asmr=asmr, look_prompt=look_prompt,
        ),
        "image_input": [persona_uri, product_uri],
        "aspect_ratio": "9:16",
    }
    client = ReplicateClient(api_key=replicate_api_key)
    out = client.run_model(MODEL_NANO, params)
    return _extract_output(out), COST_PER_IMAGE_USD


def generate_studio_portrait(
    *,
    product_image_bytes: bytes,
    product_content_type: str,
    product_name: str,
    brand: str,
    asmr: bool,
    replicate_api_key: str,
) -> tuple[bytes | str, float]:
    """Run nano-banana-pro; return (bytes-or-url, cost_usd)."""
    uri = (
        f"data:{product_content_type};base64,"
        f"{base64.b64encode(product_image_bytes).decode()}"
    )
    params = {
        "prompt": build_studio_prompt(
            product_name=product_name, brand=brand, asmr=asmr,
        ),
        "image_input": [uri],
        "aspect_ratio": "9:16",
    }
    client = ReplicateClient(api_key=replicate_api_key)
    out = client.run_model(MODEL_NANO, params)
    return _extract_output(out), COST_PER_IMAGE_USD
