"""
Mock-провайдер для unit-тестов и локальной разработки без реальных API.

Имитирует жизненный цикл: первый poll → RUNNING, второй → SUCCEEDED.
Возвращает заранее заданный sample URL.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from app.core.video_providers.base import (
    VideoProviderBase,
    GenerationRequest,
    GenerationResult,
    ProviderJobStatus,
)


SAMPLE_MP4_URL = (
    "https://commondatastorage.googleapis.com/gtv-videos-bucket/"
    "sample/BigBuckBunny.mp4"
)


class MockProvider(VideoProviderBase):
    name = "mock"

    # Хранилище состояний job-ов между вызовами. Process-local, не персистится.
    _jobs: ClassVar[dict[str, dict]] = {}

    def submit(self, request: GenerationRequest) -> GenerationResult:
        job_id = f"mock_{uuid.uuid4().hex[:12]}"
        self._jobs[job_id] = {"polls": 0, "request": request}
        return GenerationResult(
            status=ProviderJobStatus.QUEUED,
            provider_job_id=job_id,
        )

    def poll(self, provider_job_id: str) -> GenerationResult:
        state = self._jobs.get(provider_job_id)
        if state is None:
            return GenerationResult(
                status=ProviderJobStatus.FAILED,
                provider_job_id=provider_job_id,
                error_message=f"Unknown mock job_id: {provider_job_id}",
            )
        state["polls"] += 1
        request = state["request"]
        if state["polls"] == 1:
            return GenerationResult(
                status=ProviderJobStatus.RUNNING,
                provider_job_id=provider_job_id,
            )
        return GenerationResult(
            status=ProviderJobStatus.SUCCEEDED,
            provider_job_id=provider_job_id,
            media_url=SAMPLE_MP4_URL,
            thumbnail_url=None,
            duration_seconds=request.duration_seconds,
            width=request.width or 720,
            height=request.height or 1280,
            cost_usd_cents=0,
            raw_response={"mock": True, "polls": state["polls"]},
        )
