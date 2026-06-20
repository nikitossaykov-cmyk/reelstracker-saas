"""Lipsync stage — animates the still portrait into a talking-head video.

Ported from /opt/tg-bot-mimic/tools/reel_factory/bin/lipsync.py. Helper
selected `prunaai/p-video-avatar` as the default backend after a 4-model
test matrix (Pruna / OmniHuman / Kling / Veo) — Pruna is the cheapest
at ~$0.025/sec of output video and Nick judged the quality difference
to be visually irrelevant for the MakeUGC use case (handoff §58).

Replicate SDK 1.x quirks (same as portrait):
  - run() may return a FileOutput (single video) or a list of
    FileOutputs / URL strings depending on the model. We accept all.
"""
from __future__ import annotations

import base64
import logging

from app.services.replicate_client import ReplicateClient


MODEL_PRUNA = "prunaai/p-video-avatar"

# 24-second talking-head at the helper's $0.025/s tier.
COST_PER_SECOND_USD = 0.025

# Approx output duration for cost estimation. The persona script is
# always shaped towards ~24 seconds of speech; we charge against that
# even if Pruna trims silence and ends a touch shorter.
EXPECTED_OUTPUT_SECONDS = 24

log = logging.getLogger(__name__)


def _to_data_uri(blob: bytes, *, kind: str, ext: str) -> str:
    media = {"jpeg": "jpeg", "jpg": "jpeg", "png": "png",
             "mp3": "mpeg", "wav": "wav"}.get(ext, ext)
    return f"data:{kind}/{media};base64,{base64.b64encode(blob).decode()}"


def _extract_video_output(out) -> bytes | str:
    """Same polymorphism trap as portrait.generate; centralise the matrix."""
    if isinstance(out, str):
        return out
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    if hasattr(out, "read") and callable(out.read):
        return out.read()
    if isinstance(out, list) and out:
        return _extract_video_output(out[0])
    raise RuntimeError(
        f"lipsync returned unexpected output type: {type(out).__name__}"
    )


def generate_lipsync(
    *,
    portrait_bytes: bytes,
    portrait_ext: str,
    voiceover_bytes: bytes,
    voiceover_ext: str,
    replicate_api_key: str,
    video_prompt: str | None = None,
) -> tuple[bytes | str, float]:
    """Run Pruna p-video-avatar; return (bytes-or-url, cost_usd_estimate).

    Audio drives the lip-sync; resolution fixed at 720p (matching the
    final reel target). The model takes 5-8 minutes per call — caller's
    ReplicateClient must allow that long.
    """
    img_uri = _to_data_uri(portrait_bytes, kind="image", ext=portrait_ext)
    aud_uri = _to_data_uri(voiceover_bytes, kind="audio", ext=voiceover_ext)

    params: dict = {
        "image": img_uri,
        "audio": aud_uri,
        "resolution": "720p",
    }
    if video_prompt:
        params["video_prompt"] = video_prompt

    client = ReplicateClient(api_key=replicate_api_key)
    out = client.run_model(MODEL_PRUNA, params)
    return _extract_video_output(out), COST_PER_SECOND_USD * EXPECTED_OUTPUT_SECONDS
