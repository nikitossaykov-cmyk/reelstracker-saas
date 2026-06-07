"""
Voice clone через MSI XTTS-v2.

Берёт текст + reference voice (3+ сек аудио) → генерит TTS на голосе
референса в указанном языке (ru/en/etc). Бесплатно на RTX 3070.

Использует существующий CLI на MSI: `C:\\Users\\Maxim\\xtts_work\\xtts_clone.py`
(установлен в /opt/projects/reelstracker-saas в PR ранее ночью).

Pipeline:
  text + ref_voice_path → SCP ref to MSI → ssh python xtts_clone.py →
  SCP result wav back → upload to R2 → key
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


MSI_HOST = os.environ.get("MSI_HOST", "100.118.157.108")
MSI_SSH_USER = os.environ.get("MSI_SSH_USER", "Maxim")
MSI_XTTS_VENV_PY = r"C:\Users\Maxim\xtts_venv\Scripts\python.exe"
MSI_XTTS_CLI = r"C:\Users\Maxim\xtts_work\xtts_clone.py"


class XTTSError(Exception):
    pass


def _scp(local: str, remote: str, reverse: bool = False, timeout: int = 60) -> None:
    src, dst = ((f"{MSI_SSH_USER}@{MSI_HOST}:{remote}", local) if reverse
                else (local, f"{MSI_SSH_USER}@{MSI_HOST}:{remote}"))
    r = subprocess.run(
        ["scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", src, dst],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise XTTSError(f"scp {'from' if reverse else 'to'} MSI failed: {r.stderr[:200]}")


def voice_clone(
    text: str,
    ref_voice_path: Path,
    out_wav_path: Path,
    language: str = "ru",
    timeout: int = 300,
) -> Path:
    """Клонировать voice → out_wav_path.

    Возвращает path к локальному wav. Raises XTTSError при ошибке.
    """
    if not ref_voice_path.exists():
        raise XTTSError(f"ref voice not found: {ref_voice_path}")
    if len(text.strip()) == 0:
        raise XTTSError("empty text")

    job_id = uuid.uuid4().hex[:8]
    ref_remote_rel = f"xtts_work/job_{job_id}_ref.wav"
    text_remote_rel = f"xtts_work/job_{job_id}_text.txt"
    out_remote = f"C:/Users/Maxim/xtts_work/job_{job_id}_out.wav"

    text_local = Path(tempfile.mkdtemp(prefix=f"xtts_{job_id}_")) / "text.txt"
    text_local.write_text(text, encoding="utf-8")

    try:
        # Upload ref + text
        logger.info(f"xtts job {job_id}: upload to MSI")
        _scp(str(ref_voice_path), ref_remote_rel)
        _scp(str(text_local), text_remote_rel)

        # Run xtts on MSI
        cmd = (
            f'"{MSI_XTTS_VENV_PY}" "{MSI_XTTS_CLI}" '
            f'--text-file "C:/Users/Maxim/{text_remote_rel}" '
            f'--ref "C:/Users/Maxim/{ref_remote_rel}" '
            f'--lang {language} '
            f'--out "{out_remote}"'
        )
        logger.info(f"xtts job {job_id}: running on MSI ({len(text)} chars)")
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
             f"{MSI_SSH_USER}@{MSI_HOST}", cmd],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if r.returncode != 0:
            raise XTTSError(f"xtts on MSI failed (rc={r.returncode}): "
                            f"stderr={r.stderr[:300]}, stdout={r.stdout[-300:]}")

        # Download result
        logger.info(f"xtts job {job_id}: download wav")
        _scp(str(out_wav_path), out_remote, reverse=True)

        # Cleanup MSI
        subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no",
             f"{MSI_SSH_USER}@{MSI_HOST}",
             f'del "C:\\Users\\Maxim\\{ref_remote_rel.replace("/", chr(92))}" '
             f'"C:\\Users\\Maxim\\{text_remote_rel.replace("/", chr(92))}" '
             f'"{out_remote.replace("/", chr(92))}"'],
            capture_output=True, timeout=15, check=False,
        )

        if not out_wav_path.exists():
            raise XTTSError(f"output wav missing after scp: {out_wav_path}")
        return out_wav_path
    finally:
        try:
            text_local.unlink(missing_ok=True)
            text_local.parent.rmdir()
        except OSError:
            pass


def voice_clone_to_r2(
    text: str,
    ref_voice_local: Path,
    user_id: int,
    language: str = "ru",
) -> tuple[str, str]:
    """Voice clone + сразу залить в R2. Возвращает (storage_key, public_url)."""
    workdir = Path(tempfile.mkdtemp(prefix=f"xtts_r2_"))
    out_wav = workdir / "vo.wav"
    try:
        voice_clone(text, ref_voice_local, out_wav, language=language)
        from app.core.storage import get_r2
        r2 = get_r2()
        key = f"users/{user_id}/voice/{uuid.uuid4().hex[:12]}.wav"
        with out_wav.open("rb") as f:
            r2.upload_bytes(key, f.read(), content_type="audio/wav")
        url = r2.get_proxy_url(key)
        logger.info(f"voice clone uploaded to R2: {key}")
        return key, url
    finally:
        try:
            out_wav.unlink(missing_ok=True)
            workdir.rmdir()
        except OSError:
            pass
