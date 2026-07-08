"""Action cutaway inserts — cap-off + spray b-roll for the Studio reel.

Recipe proven in vault wc_spritz_clip.py / v36: nano-banana-pro still
with TWO image refs (portrait first, real product photo second) keeps
girl and bottle consistent; Kling i2v animates spray fine but renders
sniff as drinking/kissing — so only cap_off + spray, никакого sniff.
Kling STANDARD: ~11× cheaper than PRO, b-roll quality достаточная.
"""
from __future__ import annotations

import base64

from app.services.replicate_client import ReplicateClient
from app.services.strategy_makeugc.portrait import _extract_output


MODEL_STILL = "google/nano-banana-pro"
MODEL_ANIMATE = "kwaivgi/kling-v2.1"
COST_STILL_USD = 0.15
COST_CLIP_USD = 0.25  # Kling standard 5s — уточнить по факту первого прогона

KINDS = ("cap_off", "spray")


class CutawayError(Exception):
    pass


_ACTIONS = {
    "cap_off": (
        "Both her hands are raised to chest height in front of her: one "
        "hand holds the bottle, the other is lifting the matte black cap "
        "straight up off the bottle — the cap has just separated from the "
        "neck. Her eyes look down at the bottle with curiosity."
    ),
    "spray": (
        "She holds the bottle in her right hand raised toward her neck and "
        "wrist, index finger placed on top of the pump, a fine delicate "
        "mist visible against the light. Her eyes are softly closed, head "
        "tilted slightly back."
    ),
}

MOTION_PROMPTS = {
    "cap_off": (
        "One subtle continuous motion only: her hand slowly lifts the "
        "matte black cap up and away from the bottle, she watches the "
        "bottle. The bottle stays in her other hand throughout, no "
        "morphing, no warping, the label remains readable. Soft ambient "
        "indoor calm, smooth cinematic micro-motion."
    ),
    "spray": (
        "One subtle continuous motion only: her index finger presses down "
        "on the perfume pump once, a fine delicate mist sprays from the "
        "nozzle onto her wrist and neck, the mist catches the light, her "
        "eyes stay softly closed. The bottle stays in her hand throughout, "
        "no morphing, no warping, the label remains readable."
    ),
}

NEGATIVE_PROMPT = (
    "drinking the bottle, kissing the bottle, putting bottle in mouth, "
    "mouth-to-bottle, bottle to lips, eating, distorted hand, deformed "
    "fingers, extra fingers, morphing bottle, melting label, jittery, "
    "low quality, watermark"
)


def build_cutaway_still_prompt(*, kind: str, product_name: str, brand: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown cutaway kind: {kind} (allowed: {KINDS})")
    return (
        "Photorealistic vertical UGC photo: the SAME woman as in the "
        "first reference image — same face, same hair, same clothes, same "
        "room and lighting. She interacts with the EXACT perfume bottle "
        "from the second reference image — keep its shape, cap, color and "
        f"label identical; the label text must read exactly «{brand}» and "
        f"«{product_name}», do not misspell, redraw or invent any text. "
        f"{_ACTIONS[kind]} "
        "Shot on a phone, shallow depth of field, natural skin tones, "
        "vertical 9:16 composition, candid UGC aesthetic."
    )


def _data_uri(blob: bytes, content_type: str = "image/jpeg") -> str:
    return f"data:{content_type};base64,{base64.b64encode(blob).decode()}"


def generate_cutaway_still(
    *,
    portrait_bytes: bytes,
    product_bytes: bytes,
    kind: str,
    product_name: str,
    brand: str,
    replicate_api_key: str,
) -> tuple[bytes | str, float]:
    """nano-banana-pro still: portrait ref first, product ref second."""
    params = {
        "prompt": build_cutaway_still_prompt(
            kind=kind, product_name=product_name, brand=brand,
        ),
        "image_input": [_data_uri(portrait_bytes), _data_uri(product_bytes)],
        "aspect_ratio": "9:16",
        "output_format": "jpg",
    }
    client = ReplicateClient(api_key=replicate_api_key)
    out = client.run_model(MODEL_STILL, params)
    return _extract_output(out), COST_STILL_USD


def animate_cutaway(
    *,
    still_bytes: bytes,
    kind: str,
    replicate_api_key: str,
) -> tuple[bytes | str, float]:
    """Kling v2.1 standard, 5s, still as start frame."""
    if kind not in KINDS:
        raise CutawayError(f"unknown cutaway kind: {kind}")
    params = {
        "mode": "standard",
        "duration": 5,
        "prompt": MOTION_PROMPTS[kind],
        "negative_prompt": NEGATIVE_PROMPT,
        "start_image": _data_uri(still_bytes),
    }
    client = ReplicateClient(api_key=replicate_api_key)
    out = client.run_model(MODEL_ANIMATE, params)
    return _extract_output(out), COST_CLIP_USD
