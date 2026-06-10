"""
Сервис face-swap через MSI ComfyUI ReActor.

Берёт GeneratedVideo (или Reel media) → face image → отправляет на MSI,
получает результат с подменённым лицом → загружает в R2 → пишет в новое
поле generated_videos.face_swap_storage_key.

Полностью бесплатно (RTX 3070 8GB), но требует:
- MSI online (Tailscale 100.118.157.108)
- ComfyUI-ReActor установлен (уже есть)
- SSH ключи VPS→MSI (уже настроено)
"""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models.generation import GeneratedVideo
from app.core.comfy_msi import (
    ping_msi, upload_to_msi_input, submit_workflow,
    wait_for_completion, find_output_video, download_msi_output,
    cleanup_msi_input, load_workflow_template,
    MSINotReachable, ComfyWorkflowError,
)

logger = logging.getLogger(__name__)


def _r2_storage_key(user_id: int, gv_id: int, suffix: str = "faceswap") -> str:
    return f"users/{user_id}/{suffix}/{gv_id}_{uuid.uuid4().hex[:8]}.mp4"


def face_swap_via_msi(
    db: Session,
    gv: GeneratedVideo,
    face_image_local: Path,
) -> str:
    """Face-swap source video из gv через MSI ReActor.

    Pipeline:
      1. download R2 video → /tmp
      2. upload video + face to MSI input
      3. submit ReActor workflow → poll → download output
      4. upload output to R2 → save key

    Возвращает R2 key финального видео.
    """
    if not gv.media_storage_key:
        raise ValueError(f"gv #{gv.id} has no media_storage_key — generate first")
    if not ping_msi():
        raise MSINotReachable("MSI ComfyUI not reachable at 100.118.157.108:8188")

    try:
        from app.core.storage import get_r2
        r2 = get_r2()
    except Exception as e:
        raise RuntimeError(f"R2 not available: {e}")

    workdir = Path(tempfile.mkdtemp(prefix=f"fs_{gv.id}_"))
    src_video = workdir / "src.mp4"
    out_video = workdir / "swapped.mp4"
    uploaded_inputs: list[str] = []

    try:
        # 1) Download src from R2
        logger.info(f"face_swap gv #{gv.id}: download src from R2")
        r2._client.download_file(r2.bucket, gv.media_storage_key, str(src_video))

        # 2) Upload to MSI
        logger.info(f"face_swap gv #{gv.id}: upload to MSI")
        src_fname = upload_to_msi_input(src_video, f"fs_{gv.id}_src.mp4")
        face_fname = upload_to_msi_input(face_image_local, f"fs_{gv.id}_face{face_image_local.suffix}")
        uploaded_inputs = [src_fname, face_fname]

        # 3) Build & submit workflow
        wf = load_workflow_template("reactor_face_swap")
        wf["1"]["inputs"]["video"] = src_fname
        wf["2"]["inputs"]["image"] = face_fname
        wf["4"]["inputs"]["filename_prefix"] = f"ReactorSwap_{gv.id}"

        logger.info(f"face_swap gv #{gv.id}: submit ReActor workflow to MSI")
        prompt_id = submit_workflow(wf)
        logger.info(f"  prompt_id={prompt_id}, waiting...")

        # 4) Wait
        entry = wait_for_completion(prompt_id, timeout=600)
        out_fname = find_output_video(entry)
        if not out_fname:
            raise ComfyWorkflowError(f"no output video in history: {entry.get('outputs')}")
        logger.info(f"  done, output={out_fname}")

        # 5) Download from MSI
        download_msi_output(out_fname, out_video)

        # 6) Upload to R2 (ensure faststart for browser streaming)
        from app.core.faststart import ensure_faststart
        ensure_faststart(out_video)
        key = _r2_storage_key(gv.user_id, gv.id, suffix="faceswap")
        with out_video.open("rb") as f:
            r2.upload_bytes(key, f.read(), content_type="video/mp4")
        logger.info(f"face_swap gv #{gv.id}: R2 saved {key}")

        # 7) Update DB — пишем как uniq-копию (cheaper-than-runway альтернатива)
        gv.uniq_storage_key = key
        gv.uniq_media_url = r2.get_proxy_url(key)
        gv.uniqified_at = datetime.utcnow()
        db.commit()

        # cleanup MSI input
        cleanup_msi_input(uploaded_inputs)
        return key

    finally:
        for p in (src_video, out_video, face_image_local):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass
