"""
Whisper-транскрипция через OpenAI API.

Альтернатива — локальный faster-whisper, но требует ~500MB модели в pod-е
и GPU/CPU runtime. API проще: $0.006/min, ничего не качаем, ленивый импорт.

Возвращает plain text (без таймкодов — для recipe extractor'а этого хватает).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TranscribeError(Exception):
    """Любая ошибка транскрибации, нормализованная."""


def transcribe_audio(
    media_path: str | Path,
    openai_api_key: str,
    language: Optional[str] = None,
    model: str = "whisper-1",
    timeout: int = 120,
) -> str:
    """Транскрибировать аудиодорожку MP4 → текст.

    OpenAI Whisper-1 принимает m4a/mp3/mp4/mpeg/mpga/wav/webm ≤25MB.
    Для длинных или больших файлов нужно резать на чанки — пока без этого,
    рилсы обычно <60с/<10MB.
    """
    if not openai_api_key:
        raise TranscribeError("openai_api_key пуст — нечем авторизоваться")

    path = Path(media_path)
    if not path.exists():
        raise TranscribeError(f"media file not found: {path}")

    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb > 25:
        raise TranscribeError(
            f"media file {size_mb:.1f}MB > 25MB лимит Whisper-1. "
            "Резать на чанки — todo для длинных видео."
        )

    try:
        from openai import OpenAI
    except ImportError as e:
        raise TranscribeError(f"openai SDK не установлен: {e}")

    client = OpenAI(api_key=openai_api_key, timeout=timeout)
    try:
        with path.open("rb") as f:
            response = client.audio.transcriptions.create(
                model=model,
                file=f,
                language=language,  # None → автодетект; "ru"/"en" — явно
                response_format="text",
            )
    except Exception as e:
        raise TranscribeError(f"OpenAI {type(e).__name__}: {str(e)[:300]}")

    text = response if isinstance(response, str) else getattr(response, "text", "")
    logger.info(f"transcribed {path.name}: {len(text)} chars")
    return text.strip()
