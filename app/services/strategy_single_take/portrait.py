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
