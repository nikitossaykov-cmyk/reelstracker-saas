"""
Runway Gen-4 video provider — STUB ONLY (PR #1).

В этом PR — только контракт и валидация конфига. Реальная интеграция
с Runway API (POST /v1/image_to_video, polling GET /v1/tasks/<id>) —
в следующем PR.

Документация на момент написания: docs.dev.runwayml.com.
Эндпоинт-аутентификация: header `Authorization: Bearer <RUNWAYML_API_SECRET>`
+ `X-Runway-Version: 2024-11-06`.
"""

from __future__ import annotations

from app.core.video_providers.base import (
    VideoProviderBase,
    GenerationRequest,
    GenerationResult,
    ProviderJobStatus,
    ProviderError,
)


class RunwayProvider(VideoProviderBase):
    name = "runway"

    # Дефолтная Runway-модель. В реальной интеграции выбираем из
    # request.extra["model"] с фолбэком сюда.
    DEFAULT_MODEL = "gen4_turbo"

    # Версия API в header (Runway требует pinned версию).
    API_VERSION = "2024-11-06"

    BASE_URL = "https://api.dev.runwayml.com/v1"

    def __init__(self, api_key: str):
        if not api_key or not api_key.startswith("key_"):
            # Runway-ключи в их dev API имеют префикс "key_".
            # Это лёгкая sanity-проверка, не строгая валидация.
            raise ProviderError(
                "RunwayProvider: api_key looks invalid "
                "(expected 'key_...' format from runwayml.com dashboard)"
            )
        self.api_key = api_key

    def submit(self, request: GenerationRequest) -> GenerationResult:
        # STUB: реальная имплементация ожидается в PR #2 (workers + storage).
        # Текущая роль — позволить worker-у инстанциировать провайдер при
        # обработке job-а GENERATE_VIDEO и получить понятную ошибку.
        raise NotImplementedError(
            "RunwayProvider.submit() — real HTTP call landing in PR #2"
        )

    def poll(self, provider_job_id: str) -> GenerationResult:
        raise NotImplementedError(
            "RunwayProvider.poll() — real HTTP call landing in PR #2"
        )
