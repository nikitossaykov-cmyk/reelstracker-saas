"""
Абстрактный интерфейс видео-генеративного провайдера.

Все имплементации (Runway/Pika/Hailuo/Mock) должны соответствовать
этому контракту. Используется worker-ом GENERATE_VIDEO job-type.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


class ProviderJobStatus(str, enum.Enum):
    """Статус задачи на стороне провайдера. Нормализуем разнородные
    провайдерские статусы в этот набор."""
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class GenerationRequest:
    """Параметры одной генерации. Передаются в submit()."""
    prompt: str
    # Опциональный image-to-video reference (URL или путь).
    init_image_url: Optional[str] = None
    # Желаемая длительность в секундах. Провайдер может ограничить.
    duration_seconds: int = 5
    # Соотношение сторон: "9:16" (TikTok/Reels), "16:9", "1:1".
    aspect_ratio: str = "9:16"
    # Желаемое разрешение (минимальное). Провайдер может вернуть выше.
    width: Optional[int] = None
    height: Optional[int] = None
    seed: Optional[int] = None
    # Доп. параметры провайдер-специфичные (model name, motion strength и т.д.).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class GenerationResult:
    """Результат одной попытки опроса статуса генерации."""
    status: ProviderJobStatus
    # Провайдерский ID задачи (для последующих poll-ов).
    provider_job_id: str
    # Когда status == SUCCEEDED:
    media_url: Optional[str] = None             # прямая ссылка скачать .mp4
    thumbnail_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    # Реальная стоимость, списанная провайдером (в их валюте — обычно USD-cents).
    cost_usd_cents: Optional[int] = None
    # Сырой ответ провайдера для debug (опционально).
    raw_response: Optional[dict[str, Any]] = None
    # Когда status == FAILED:
    error_message: Optional[str] = None


class ProviderError(Exception):
    """Базовая ошибка для всех провайдер-проблем."""


class InsufficientFundsError(ProviderError):
    """У юзера на стороне провайдера закончились кредиты/баланс."""


class InvalidPromptError(ProviderError):
    """Промпт отклонён модерацией провайдера."""


class VideoProviderBase(ABC):
    """Контракт для всех генеративных провайдеров.

    Жизненный цикл:
        provider = get_provider("runway", api_key=user.runway_api_key)
        result = provider.submit(GenerationRequest(prompt="..."))
        # result.status в {QUEUED, RUNNING}
        # сохраняем result.provider_job_id в GeneratedVideo.provider_job_id

        # ... через 10-60 секунд worker опрашивает:
        result = provider.poll(provider_job_id)
        # if result.status == SUCCEEDED → скачать result.media_url → S3
        # if result.status == FAILED → пометить GeneratedVideo как FAILED
    """

    name: str = "base"

    @abstractmethod
    def submit(self, request: GenerationRequest) -> GenerationResult:
        """Поставить задачу на генерацию. Возвращает QUEUED или RUNNING.

        Должен бросать InsufficientFundsError / InvalidPromptError /
        ProviderError по ситуации.
        """
        ...

    @abstractmethod
    def poll(self, provider_job_id: str) -> GenerationResult:
        """Опросить статус задачи. Idempotent — можно вызывать сколько угодно.

        Когда status == SUCCEEDED, заполнены media_url и метаданные.
        Когда status == FAILED, заполнено error_message.
        """
        ...

    def cancel(self, provider_job_id: str) -> None:
        """Опционально — отменить задачу. По умолчанию no-op."""
        return
