"""
Content Forge analyzers — извлечение структуры из скачанного MP4-рилса.

Каждый аналайзер опционален и работает независимо: даже частичный анализ
полезен (например, transcript без vision summary всё ещё помогает recipe
extractor'у). При ошибке провайдера задача не падает, а пишет error_message.
"""

from app.core.analyzers.transcriber import transcribe_audio, TranscribeError
from app.core.analyzers.vision import summarize_video, VisionError
from app.core.analyzers.scenes import detect_scenes, SceneError
from app.core.analyzers.classifier import classify_hook, ClassifyError

__all__ = [
    "transcribe_audio", "TranscribeError",
    "summarize_video", "VisionError",
    "detect_scenes", "SceneError",
    "classify_hook", "ClassifyError",
]
