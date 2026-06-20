"""TTS voiceover via the shared ElevenLabs Instant Voice Clone.

Ported from /opt/tg-bot-mimic/tools/reel_factory/bin/tts.py (eleven
backend only — XTTS / OpenAI backends not relevant for MakeUGC where
quality matters and shared voice is fixed).

Reads the shared key from ELEVENLABS_API_KEY env (set on Railway), and
the cloned voice from MAKEUGC_DEFAULT_VOICE_ID. Per-user override of
either of these lands when we wire the Settings UI for it.

Settings (stability=0.2 / similarity_boost=0.6 / style=0.75) were
picked by Nick after a three-voice A/B/C test on 2026-06-17.
"""
from __future__ import annotations

import json
import os
import urllib.request


TTS_MODEL = "eleven_multilingual_v2"


class VoiceoverError(RuntimeError):
    pass


class QuotaExceededError(VoiceoverError):
    """Raised when shared quota for the calling user is exhausted this month."""


def resolve_api_key(user_key: str | None) -> str | None:
    """Per-user key wins; falls back to shared ELEVENLABS_API_KEY env."""
    return user_key or os.getenv("ELEVENLABS_API_KEY")


def resolve_voice_id(user_voice_id: str | None) -> str | None:
    """Per-user voice id wins; falls back to MAKEUGC_DEFAULT_VOICE_ID env."""
    return user_voice_id or os.getenv("MAKEUGC_DEFAULT_VOICE_ID")


def generate_voiceover(
    *,
    script_text: str,
    voice_id: str,
    api_key: str,
) -> bytes:
    """Run ElevenLabs TTS, return raw MP3 bytes."""
    if not voice_id:
        raise VoiceoverError("voice_id is empty")
    if not api_key:
        raise VoiceoverError("api_key is empty")
    if not script_text or not script_text.strip():
        raise VoiceoverError("script_text is empty")

    body = {
        "text": script_text,
        "model_id": TTS_MODEL,
        # Tuned 2026-06-20 round 2 — at style=0.95 the model drifted
        # into Chinese mid-sentence on v6 (a known eleven_multilingual
        # quirk when style is near max + foreign tokens like the brand
        # transliteration "Бакара руж" sit next to russian numerals).
        # Back off to style=0.65 + stability=0.3 — still much livelier
        # than v4's calm 0.2/0.75 baseline, but anchored enough to
        # stay in Russian end-to-end.
        "voice_settings": {
            "stability": 0.3,
            "similarity_boost": 0.55,
            "style": 0.65,
            "use_speaker_boost": True,
        },
        "language_code": "ru",
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
        if e.code == 401:
            raise VoiceoverError(
                f"ElevenLabs unauthorized — bad/expired key: {body_text}"
            ) from e
        if e.code == 429:
            raise VoiceoverError(
                f"ElevenLabs rate-limited: {body_text}"
            ) from e
        raise VoiceoverError(
            f"ElevenLabs HTTP {e.code}: {body_text}"
        ) from e
