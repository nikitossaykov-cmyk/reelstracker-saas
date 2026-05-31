"""
Cost calculator — оценочные цены по тарифным сеткам провайдеров.

Все цены в USD-cents (целое), храним в GeneratedVideo.cost_kopecks (имя
поля legacy, но семантика = USD cents).

Источники (на 2026-05-31):
- Runway dev API: gen4.5 ≈ 5 credits/sec ≈ $0.05/sec (5 cents);
  gen4_turbo ≈ $0.05/sec; veo3.1_fast ≈ $0.40/sec.
- OpenAI Whisper-1: $0.006/min audio.
- OpenAI gpt-4o-mini: $0.15/M input tokens, $0.60/M output tokens.
- Vision: gpt-4o-mini image (low detail) ≈ 85 tokens/image.
- Cloudflare R2: storage free first 10GB, egress free.
"""

from __future__ import annotations

from typing import Optional


RUNWAY_USD_CENTS_PER_SEC = {
    "gen4.5":       5,    # $0.05/sec
    "gen4_turbo":   5,
    "veo3.1_fast":  40,   # $0.40/sec
}


def runway_cost_cents(model: str, duration_sec: int) -> int:
    """Цена одного Runway gen call в USD-cents."""
    rate = RUNWAY_USD_CENTS_PER_SEC.get(model, 5)
    return rate * max(1, int(duration_sec))


def whisper_cost_cents(audio_sec: float) -> int:
    """Whisper-1: $0.006/min. Round up to 0.01 cents."""
    return max(1, round(audio_sec / 60 * 0.6))  # 0.6 cents per min


def gpt4o_mini_cost_cents(input_tokens: int, output_tokens: int) -> int:
    """gpt-4o-mini USD-cents."""
    cost = (input_tokens / 1_000_000 * 15) + (output_tokens / 1_000_000 * 60)
    return max(1, round(cost))


def vision_classify_cost_cents(num_scenes: int) -> int:
    """Грубая оценка scene classifier: ~85 image tokens + 200 input + 100
    output на сцену → ~$0.001/scene."""
    return max(1, round(num_scenes * 0.1))  # 0.1 cent per scene


def recipe_extract_cost_cents() -> int:
    """One recipe extraction call: ~1500 input + 2000 output tokens
    gpt-4o-mini ≈ $0.003."""
    return 3  # 0.3 cents — округлим до 3 cents для запаса


def analyzer_cost_cents(audio_duration_sec: float = 0.0,
                        vision_frames: int = 6) -> int:
    """Whisper + Vision summary cost."""
    w = whisper_cost_cents(audio_duration_sec) if audio_duration_sec else 0
    # Vision: 6 frames @ low + ~500 input + ~300 output = ~$0.0006
    v = max(1, round(vision_frames * 0.05))
    return w + v


def format_cost_usd(cents: int) -> str:
    """Format cents → '$0.52'."""
    if cents is None:
        return "—"
    return f"${cents / 100:.2f}"


def cost_breakdown(
    *,
    analyzer_audio_sec: float = 0.0,
    analyzer_frames: int = 0,
    scene_classify_count: int = 0,
    recipe_count: int = 0,
    runway_chunks: Optional[list[dict]] = None,
) -> dict:
    """Собрать полный breakdown costs для одного hybrid-remake job.

    runway_chunks: list of {model: str, duration_sec: int}.
    """
    chunks = runway_chunks or []
    parts = {
        "analyzer_whisper_vision": analyzer_cost_cents(analyzer_audio_sec, analyzer_frames),
        "scene_classifier":         vision_classify_cost_cents(scene_classify_count),
        "recipe_extractor":         recipe_extract_cost_cents() if recipe_count else 0,
        "runway_chunks":            sum(runway_cost_cents(c["model"], c["duration_sec"]) for c in chunks),
    }
    parts["total"] = sum(parts.values())
    parts["total_usd"] = format_cost_usd(parts["total"])
    parts["chunks_detail"] = [
        {"model": c["model"], "duration_sec": c["duration_sec"],
         "cents": runway_cost_cents(c["model"], c["duration_sec"])}
        for c in chunks
    ]
    return parts
