"""
Runway Gen-4 / Veo 3.1 (через Runway) video provider.

Использует Runway dev API (https://docs.dev.runwayml.com/).
Заголовки: Authorization: Bearer <key_*>, X-Runway-Version: 2024-11-06.

Endpoints:
    POST /v1/text_to_video  → {id}
    GET  /v1/tasks/{id}      → {id, status, progress, output?, failure?}

Статусы Runway: PENDING / THROTTLED / RUNNING / SUCCEEDED / FAILED / CANCELLED.
Маппим в наш ProviderJobStatus.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.video_providers.base import (
    VideoProviderBase,
    GenerationRequest,
    GenerationResult,
    ProviderJobStatus,
    ProviderError,
    InsufficientFundsError,
    InvalidPromptError,
)

logger = logging.getLogger(__name__)


_STATUS_MAP = {
    "PENDING": ProviderJobStatus.QUEUED,
    "THROTTLED": ProviderJobStatus.QUEUED,
    "RUNNING": ProviderJobStatus.RUNNING,
    "SUCCEEDED": ProviderJobStatus.SUCCEEDED,
    "FAILED": ProviderJobStatus.FAILED,
    "CANCELLED": ProviderJobStatus.CANCELLED,
}


def _ratio_for(model: str, aspect_ratio: str) -> str:
    """Перевести наш aspect_ratio ('9:16'|'16:9'|'1:1') в Runway-ratio.

    Runway требует разные форматы для разных моделей: gen4_* любит
    '720:1280', veo3.1_fast — '1280:720' / '720:1280'. Для MVP — фиксированный
    набор popular ratios на стороне Runway-side."""
    presets = {
        "9:16": "720:1280",
        "16:9": "1280:720",
        "1:1":  "960:960",
    }
    return presets.get(aspect_ratio, aspect_ratio)


class RunwayProvider(VideoProviderBase):
    name = "runway"
    DEFAULT_MODEL = "gen4.5"
    API_VERSION = "2024-11-06"
    BASE_URL = "https://api.dev.runwayml.com/v1"

    def __init__(self, api_key: str, timeout: int = 60):
        if not api_key or not api_key.startswith("key_"):
            raise ProviderError(
                "RunwayProvider: api_key looks invalid "
                "(expected 'key_...' format from runwayml.com dashboard)"
            )
        self.api_key = api_key
        self.timeout = timeout

    # -- HTTP helpers --

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-Runway-Version": self.API_VERSION,
            "Content-Type": "application/json",
        }

    def _raise_for_runway_error(self, status_code: int, body: Any) -> None:
        """Конвертировать HTTP-ошибку Runway в наши исключения."""
        msg = body if isinstance(body, str) else (body.get("error") or str(body))
        if status_code == 402:
            raise InsufficientFundsError(f"Runway: insufficient credits — {msg}")
        if status_code == 400 and isinstance(msg, str) and "moderat" in msg.lower():
            raise InvalidPromptError(f"Runway moderation rejected: {msg}")
        if status_code in (401, 403):
            raise ProviderError(f"Runway auth failed ({status_code}): {msg}")
        raise ProviderError(f"Runway HTTP {status_code}: {msg}")

    # -- VideoProviderBase impl --

    def submit(self, request: GenerationRequest) -> GenerationResult:
        import requests
        model = (request.extra.get("model") if request.extra else None) or self.DEFAULT_MODEL
        ratio = (request.extra.get("ratio") if request.extra else None) \
            or _ratio_for(model, request.aspect_ratio)
        # Runway hard limit on promptText: 1000 chars (validation 400)
        prompt = request.prompt or ""
        if len(prompt) > 1000:
            logger.warning(f"Runway: trimming prompt {len(prompt)} → 1000 chars")
            prompt = prompt[:997] + "..."
        payload: dict[str, Any] = {
            "model": model,
            "promptText": prompt,
            "ratio": ratio,
            "duration": request.duration_seconds,
        }
        if request.seed is not None:
            payload["seed"] = request.seed
        if request.init_image_url:
            # image-to-video endpoint instead of text-to-video
            payload["promptImage"] = request.init_image_url
            url = f"{self.BASE_URL}/image_to_video"
        else:
            url = f"{self.BASE_URL}/text_to_video"
        logger.info(f"Runway submit → {url}, model={model}, ratio={ratio}, "
                    f"duration={request.duration_seconds}")
        r = requests.post(url, headers=self._headers(), json=payload, timeout=self.timeout)
        try:
            body = r.json()
        except Exception:
            body = r.text
        if r.status_code >= 400:
            self._raise_for_runway_error(r.status_code, body)
        if not isinstance(body, dict) or "id" not in body:
            raise ProviderError(f"Runway: unexpected submit response: {body}")
        return GenerationResult(
            status=ProviderJobStatus.QUEUED,
            provider_job_id=body["id"],
            raw_response=body,
        )

    def poll(self, provider_job_id: str) -> GenerationResult:
        import requests
        url = f"{self.BASE_URL}/tasks/{provider_job_id}"
        r = requests.get(url, headers=self._headers(), timeout=self.timeout)
        try:
            body = r.json()
        except Exception:
            body = r.text
        if r.status_code == 404:
            return GenerationResult(
                status=ProviderJobStatus.FAILED,
                provider_job_id=provider_job_id,
                error_message="Runway task not found (deleted or wrong id)",
            )
        if r.status_code >= 400:
            self._raise_for_runway_error(r.status_code, body)
        if not isinstance(body, dict):
            raise ProviderError(f"Runway: unexpected poll response: {body}")
        raw_status = body.get("status", "")
        status = _STATUS_MAP.get(raw_status, ProviderJobStatus.RUNNING)
        result = GenerationResult(
            status=status,
            provider_job_id=provider_job_id,
            raw_response=body,
        )
        if status == ProviderJobStatus.SUCCEEDED:
            output = body.get("output") or []
            if isinstance(output, list) and output:
                result.media_url = output[0]
            elif isinstance(output, str):
                result.media_url = output
            else:
                raise ProviderError(
                    f"Runway SUCCEEDED but no output URL: {body}"
                )
        elif status == ProviderJobStatus.FAILED:
            result.error_message = (
                body.get("failure") or body.get("failureCode") or "Runway: unknown failure"
            )
        return result

    def cancel(self, provider_job_id: str) -> None:
        import requests
        url = f"{self.BASE_URL}/tasks/{provider_job_id}"
        try:
            requests.delete(url, headers=self._headers(), timeout=self.timeout)
        except Exception as e:
            logger.warning(f"Runway cancel {provider_job_id} failed: {e}")
