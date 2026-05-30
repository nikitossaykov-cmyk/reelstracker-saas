"""
Cloudflare R2 (S3-compatible) storage wrapper.

Хранит сгенерированные видео и связанную медиа. Используется generation_worker
для загрузки результата от Runway → отдачи публичного URL для Graph API.

Lazy-init: клиент создаётся при первом использовании, чтобы импорт модуля
не падал в окружениях без сконфигурированного R2.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


class R2NotConfigured(RuntimeError):
    """R2 env vars не заданы — generation MVP отключён."""


class R2Storage:
    """Тонкая обёртка над boto3 S3-клиентом, нацеленным на R2."""

    def __init__(self, account_id: str, bucket: str, endpoint: str,
                 access_key_id: str, secret_access_key: str,
                 public_base_url: str = "",
                 presigned_ttl_seconds: int = 60 * 60 * 24 * 6):
        if not all([account_id, bucket, endpoint, access_key_id, secret_access_key]):
            raise R2NotConfigured(
                "R2 не сконфигурирован — задайте R2_ACCOUNT_ID, R2_BUCKET, "
                "R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY в .env"
            )
        self.account_id = account_id
        self.bucket = bucket
        self.endpoint = endpoint
        self.public_base_url = public_base_url.rstrip("/") + "/" if public_base_url else ""
        self.presigned_ttl_seconds = presigned_ttl_seconds
        # Lazy boto3 import — пакет может быть не установлен в окружениях без MVP.
        import boto3  # noqa: WPS433
        from botocore.config import Config
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
        )

    def upload_bytes(self, key: str, data: bytes, content_type: str = "video/mp4") -> str:
        """Залить bytes в R2 → вернуть storage key (не URL)."""
        self._client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
        )
        logger.info(f"R2 upload OK: s3://{self.bucket}/{key} ({len(data)} bytes)")
        return key

    def upload_from_url(self, source_url: str, key: str,
                        content_type: str = "video/mp4",
                        timeout: int = 300) -> tuple[str, int]:
        """Скачать файл по source_url → залить в R2 → вернуть (key, size_bytes).

        Стримит чанками чтобы не держать весь файл в памяти (видео может быть 10+MB).
        """
        import requests
        with requests.get(source_url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            buf = BytesIO()
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    buf.write(chunk)
            data = buf.getvalue()
        self.upload_bytes(key, data, content_type=content_type)
        return key, len(data)

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
            logger.info(f"R2 delete OK: s3://{self.bucket}/{key}")
        except Exception as e:
            logger.warning(f"R2 delete '{key}' failed: {e}")

    def get_public_url(self, key: str) -> str:
        """Вернуть публично-достижимый URL.

        Если задан R2_PUBLIC_BASE_URL (Cloudflare CDN на custom domain) —
        склеиваем с ним. Иначе генерим presigned URL с TTL.
        """
        if self.public_base_url:
            return urljoin(self.public_base_url, key)
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self.presigned_ttl_seconds,
        )


_instance: Optional[R2Storage] = None


def get_r2() -> R2Storage:
    """Lazy singleton — настраивается из app.config при первом вызове."""
    global _instance
    if _instance is None:
        from app.config import get_settings
        s = get_settings()
        _instance = R2Storage(
            account_id=s.R2_ACCOUNT_ID,
            bucket=s.R2_BUCKET,
            endpoint=s.R2_ENDPOINT,
            access_key_id=s.R2_ACCESS_KEY_ID,
            secret_access_key=s.R2_SECRET_ACCESS_KEY,
            public_base_url=s.R2_PUBLIC_BASE_URL,
            presigned_ttl_seconds=s.R2_PRESIGNED_TTL_SECONDS,
        )
    return _instance


def reset_r2() -> None:
    """Сбросить singleton (для тестов / при смене env)."""
    global _instance
    _instance = None
