"""Portrait stage — Flux Kontext generates an AI persona holding the product.

Ported from /opt/tg-bot-mimic/tools/reel_factory/bin/portrait.py. Drops
the CLI surface and rewrites token-loading to use a key passed in by the
worker (per-user Replicate keys in our schema).

`flux-kontext-max` is the default — cheap (~$0.04/img) and good enough
for the wide-shot AI persona scene. `flux-kontext-pro` is available for
"hero" shots where label legibility matters more (close-up bottle).
"""
from __future__ import annotations

import base64

from app.services.replicate_client import ReplicateClient


MODEL_MAX = "black-forest-labs/flux-kontext-max"
MODEL_PRO = "black-forest-labs/flux-kontext-pro"

COST_PER_IMAGE_USD = {MODEL_MAX: 0.04, MODEL_PRO: 0.06}


# Persona-style → prompt template. Tuned from helper-bot's manual test
# matrix (Arina-style works best for anti-glamour authenticity, which
# matches the makeugc.ai reference look that converts on IG).
STYLE_PROMPTS: dict[str, str] = {
    "average-girl": (
        "Average ordinary European girl, 23-27 years old, plain face with "
        "natural blemishes and visible pores, no makeup look, basic cream "
        "cotton t-shirt, messy bedroom with unmade bed and soft window "
        "light from the side, sitting on the edge of the bed, holding this "
        "exact perfume bottle up close with both hands, slight genuine smile, "
        "looking at the camera, UGC selfie aesthetic, shallow depth of field, "
        "vertical portrait composition. Keep the perfume bottle and its "
        "label identical to the input image."
    ),
    "glam-blonde": (
        "Stylish European blonde woman, 26-30 years old, long wavy hair, "
        "polished evening makeup, silk camisole, beige sofa with linen "
        "cushions, warm golden hour light, holding this exact perfume "
        "bottle up close with both hands, confident half-smile, looking "
        "at the camera, premium beauty editorial selfie aesthetic, shallow "
        "depth of field, vertical portrait composition. Keep the perfume "
        "bottle and its label identical to the input image."
    ),
    "brunette-glasses": (
        "European brunette woman, 28-32 years old, straight dark hair, "
        "thin gold-frame glasses, neutral knit sweater, soft daylight in "
        "a minimalist apartment, holding this exact perfume bottle close "
        "to the camera with both hands, thoughtful smile, looking at the "
        "camera, lifestyle reviewer aesthetic, shallow depth of field, "
        "vertical portrait composition. Keep the perfume bottle and its "
        "label identical to the input image."
    ),
    "older-tester": (
        "Mature European woman, 38-44 years old, natural shoulder-length "
        "hair with subtle highlights, light makeup, beige cashmere "
        "sweater, kitchen counter with morning daylight, holding this "
        "exact perfume bottle close to the camera with both hands, warm "
        "genuine smile, looking at the camera, honest reviewer aesthetic, "
        "shallow depth of field, vertical portrait composition. Keep the "
        "perfume bottle and its label identical to the input image."
    ),
}


VALID_STYLES = set(STYLE_PROMPTS.keys())


def build_portrait_prompt(persona_style: str) -> str:
    if persona_style not in STYLE_PROMPTS:
        raise ValueError(
            f"unknown persona_style: {persona_style!r} (have {sorted(VALID_STYLES)})"
        )
    return STYLE_PROMPTS[persona_style]


def _image_bytes_to_data_uri(blob: bytes, content_type: str = "image/jpeg") -> str:
    return f"data:{content_type};base64,{base64.b64encode(blob).decode()}"


def generate_portrait(
    *,
    product_image_bytes: bytes,
    product_content_type: str,
    persona_style: str,
    replicate_api_key: str,
    model: str = MODEL_MAX,
) -> tuple[str, float]:
    """Run Flux Kontext with the product image as input_image.

    Returns (result_url, cost_usd).
    """
    prompt = build_portrait_prompt(persona_style)
    product_uri = _image_bytes_to_data_uri(product_image_bytes, product_content_type)

    client = ReplicateClient(api_key=replicate_api_key)
    out = client.run_model(
        model,
        {
            "input_image": product_uri,
            "prompt": prompt,
            "aspect_ratio": "9:16",
            "output_format": "jpg",
            "safety_tolerance": 6,
        },
    )
    url = out if isinstance(out, str) else (list(out) or [None])[0]
    if not url:
        raise RuntimeError("flux-kontext returned empty output")

    return url, COST_PER_IMAGE_USD.get(model, 0.04)
