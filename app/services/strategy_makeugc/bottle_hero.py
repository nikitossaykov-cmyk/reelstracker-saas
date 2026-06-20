"""Bottle-hero close-up generator.

Flux Kontext PRO (not max) for the cutaway scene — at close-up the
bottle fills ~60% of the frame, label letters end up at ~15-20px and
Flux can actually render them legibly (helper validated through 7
iterations, handoff §75-78). MAX cannot — at wide-shot the same letters
are only 4-5px and come out as garbage shapes.

Cost: ~$0.06 / image.

Used as a fallback when the user did not upload their own real B-roll
of the product. The real-video B-roll path (PR5+ Settings UI) bypasses
this generator entirely.
"""
from __future__ import annotations

from app.services.replicate_client import ReplicateClient
from app.services.strategy_makeugc.portrait import (
    MODEL_PRO,
    _extract_output,
    _image_bytes_to_data_uri,
)


COST_USD = 0.06


BOTTLE_HERO_PROMPT = (
    "Close-up product shot of this exact small 30 ml perfume bottle "
    "(palm-sized, compact, fits in one hand — NOT large, NOT 100 ml). "
    "The bottle fills about 60% of the frame, vertical 9:16 "
    "composition. Soft warm window light from the side, the bottle is "
    "held up by a feminine hand with simple short nails — the bottle "
    "is clearly small relative to the hand, fingers wrap easily around "
    "it. Slightly blurred neutral beige bedroom background. The label "
    "is clearly readable, the glass catches gentle highlights, "
    "premium-yet-honest UGC aesthetic. Keep the perfume bottle and its "
    "label identical to the input image — same colors, same text, same "
    "proportions."
)


def generate_bottle_hero(
    *,
    product_image_bytes: bytes,
    product_content_type: str,
    replicate_api_key: str,
) -> tuple[bytes | str, float]:
    """Run Flux Kontext PRO; return (bytes-or-url, cost_usd)."""
    product_uri = _image_bytes_to_data_uri(
        product_image_bytes, product_content_type
    )
    client = ReplicateClient(api_key=replicate_api_key)
    out = client.run_model(
        MODEL_PRO,
        {
            "input_image": product_uri,
            "prompt": BOTTLE_HERO_PROMPT,
            "aspect_ratio": "9:16",
            "output_format": "jpg",
            "safety_tolerance": 6,
        },
    )
    return _extract_output(out), COST_USD
