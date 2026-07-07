"""Studio TTS — eleven_v3 with the vault-validated whisper settings.

stability 0.30 / style 0.85 / similarity 0.85 were picked on the WC
single-take iterations (v31–v36); eleven_v3 honors inline audio tags
like [whispers], which is how the ASMR voice style is produced.
Do NOT send language_code — eleven_v3 rejects it.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from app.services.strategy_makeugc.voiceover import VoiceoverError


TTS_MODEL_V3 = "eleven_v3"

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def apply_asmr_tags(text: str) -> str:
    """Prefix each sentence with [whispers] unless it already carries a tag."""
    parts = _SENT_SPLIT_RE.split(text.strip())
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p.startswith("["):
            out.append(p)
        else:
            out.append(f"[whispers] {p}")
    return " ".join(out)


def generate_voiceover_v3(
    *,
    script_text: str,
    voice_id: str,
    api_key: str,
    asmr: bool,
) -> bytes:
    """Run ElevenLabs eleven_v3 TTS, return raw MP3 bytes."""
    if not voice_id:
        raise VoiceoverError("voice_id is empty")
    if not api_key:
        raise VoiceoverError("api_key is empty")
    if not script_text or not script_text.strip():
        raise VoiceoverError("script_text is empty")

    text = apply_asmr_tags(script_text) if asmr else script_text
    body = {
        "text": text,
        "model_id": TTS_MODEL_V3,
        "voice_settings": {
            "stability": 0.30,
            "style": 0.85,
            "similarity_boost": 0.85,
            "use_speaker_boost": True,
        },
    }
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:400]
        raise VoiceoverError(f"ElevenLabs HTTP {e.code}: {body_text}") from e
