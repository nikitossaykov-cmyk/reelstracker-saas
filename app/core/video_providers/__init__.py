"""
Video generation providers.

Каждый провайдер имплементирует одинаковый интерфейс (base.VideoProvider),
чтобы worker мог переключаться между Runway / Pika / Hailuo / Mock без
изменения логики очереди.

PR #1: только абстракция + Runway stub (без реального API call) + Mock
для тестов. Реальные API-интеграции — в следующих PR.
"""

from app.core.video_providers.base import (
    VideoProviderBase,
    GenerationRequest,
    GenerationResult,
    ProviderJobStatus,
    ProviderError,
    InsufficientFundsError,
    InvalidPromptError,
)
from app.core.video_providers.mock import MockProvider
from app.core.video_providers.runway import RunwayProvider

__all__ = [
    "VideoProviderBase",
    "GenerationRequest",
    "GenerationResult",
    "ProviderJobStatus",
    "ProviderError",
    "InsufficientFundsError",
    "InvalidPromptError",
    "MockProvider",
    "RunwayProvider",
    "get_provider",
]


def get_provider(name: str, api_key: str | None = None) -> VideoProviderBase:
    """Factory — вернуть экземпляр провайдера по имени.

    Имена матчатся со значениями enum VideoProvider в моделях:
    'runway', 'pika', 'hailuo', 'veo', 'mock'.
    """
    name = name.lower()
    if name == "mock":
        return MockProvider()
    if name == "runway":
        if not api_key:
            raise ValueError("RunwayProvider requires api_key (user.runway_api_key)")
        return RunwayProvider(api_key=api_key)
    raise NotImplementedError(f"Provider '{name}' not implemented yet")
