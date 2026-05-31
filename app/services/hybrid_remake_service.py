"""
Hybrid remake orchestrator (PR #21).

Превращает source reel в ремейк, **переиспользуя оригинал по частям**:
- talking_head сегменты → Runway image_to_video с подменой
- screenshot/b_roll → cut из оригинала as-is
- text_card → ffmpeg drawtext с нашим текстом

В конце ffmpeg concat → R2 → GeneratedVideo.media_url.

Стратегия cost-optimised: на 23-сек источнике с 6 сценами получим
2-3 Runway calls (talking heads) + 3-4 free cuts = ~$0.50 vs Pure
6-chunk Runway = ~$1.20.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from sqlalchemy.orm import Session

from app.models.reel import Reel
from app.models.user import User
from app.models.recipe import ContentRecipe
from app.models.generation import GeneratedVideo, GenerationStatus, VideoProvider
from app.core.video_providers import get_provider, GenerationRequest, ProviderJobStatus
from app.core.scene_classifier import classify_scenes
from app.core.composer import RemakeParams

logger = logging.getLogger(__name__)

RUNWAY_POLL_INTERVAL = 8
RUNWAY_POLL_TIMEOUT = 600


def _ffprobe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    try:
        return float(r.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


def _cut_segment(src: Path, start: float, duration: float, out: Path) -> None:
    """Вырезать сегмент из source через ffmpeg (быстрый stream copy)."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", str(start), "-i", str(src),
         "-t", str(duration), "-c:v", "libx264", "-c:a", "aac",
         "-preset", "fast", "-crf", "20",
         str(out)],
        check=True, timeout=120,
    )


def _grab_init_frame(src: Path, start: float, out_jpg: Path) -> bool:
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", str(start + 0.05), "-i", str(src),
         "-frames:v", "1", "-q:v", "2", "-vf", "scale=720:-2", str(out_jpg)],
        capture_output=True, timeout=30, check=False,
    )
    return r.returncode == 0 and out_jpg.exists()


def _upload_frame_to_r2(jpg: Path, user_id: int) -> str:
    from app.core.storage import get_r2
    r2 = get_r2()
    key = f"users/{user_id}/hybrid_init/{uuid.uuid4().hex[:12]}.jpg"
    with jpg.open("rb") as f:
        r2.upload_bytes(key, f.read(), content_type="image/jpeg")
    return r2.get_public_url(key)


def _text_card_video(text: str, duration: float, out: Path,
                     width: int = 720, height: int = 1280) -> None:
    """Сгенерить static-text card через ffmpeg (для type=text_card)."""
    safe = text.replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")[:200]
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d={duration}",
         "-vf", f"drawtext=text='{safe}':fontcolor=white:fontsize=42:"
                f"x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=15",
         "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-t", str(duration), str(out)],
        check=True, timeout=60,
    )


def _runway_chunk(
    src: Path, scene: dict, recipe_canonical: str,
    params: RemakeParams, user: User, out: Path,
    model: str = "gen4.5",
) -> None:
    """Runway image_to_video для одной сцены — init_image = первый кадр сцены.

    Длительность округляется к {5, 10} (Runway gen4.5 поддерживает только эти).
    """
    workdir = out.parent
    init_jpg = workdir / f"init_{scene['start']:.1f}.jpg"
    if not _grab_init_frame(src, scene["start"], init_jpg):
        raise RuntimeError(f"can't grab init frame for scene @ {scene['start']}")
    init_url = _upload_frame_to_r2(init_jpg, user.id)
    try: init_jpg.unlink()
    except OSError: pass

    runway_dur = 10 if scene["duration"] > 7 else 5
    # Per-scene prompt: combine recipe context with this scene's specifics
    scene_prompt = (
        f"Scene {scene.get('start', 0):.0f}s: {scene.get('description', '')[:200]}. "
        f"{(recipe_canonical or '')[:600]} "
        f"Featuring {params.face_description or 'the original person'} "
        f"with {params.product_description or 'the product'} from "
        f"{params.brand or 'the brand'}."
    )[:1000]

    provider = get_provider("runway", api_key=user.runway_api_key)
    submit = provider.submit(GenerationRequest(
        prompt=scene_prompt,
        init_image_url=init_url,
        duration_seconds=runway_dur,
        aspect_ratio="9:16",
        extra={"model": model},
    ))
    logger.info(f"  runway chunk @ {scene['start']:.1f}s submitted: {submit.provider_job_id}")

    # Poll
    deadline = time.time() + RUNWAY_POLL_TIMEOUT
    final_url: Optional[str] = None
    while time.time() < deadline:
        time.sleep(RUNWAY_POLL_INTERVAL)
        poll = provider.poll(submit.provider_job_id)
        if poll.status == ProviderJobStatus.SUCCEEDED:
            final_url = poll.media_url
            break
        if poll.status in (ProviderJobStatus.FAILED, ProviderJobStatus.CANCELLED):
            raise RuntimeError(f"runway chunk {submit.provider_job_id}: {poll.error_message}")
    if not final_url:
        raise RuntimeError(f"runway chunk timeout: {submit.provider_job_id}")

    # Download
    r = requests.get(final_url, stream=True, timeout=120)
    r.raise_for_status()
    with out.open("wb") as f:
        for chunk in r.iter_content(1024 * 256):
            f.write(chunk)

    # Trim to scene.duration if Runway gave 5/10 sec but scene was 3
    if scene["duration"] < runway_dur - 0.5:
        trimmed = out.parent / f"trim_{out.name}"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", str(out), "-t", str(scene["duration"]),
             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             str(trimmed)],
            check=True, timeout=60,
        )
        trimmed.rename(out)


def _concat_segments(segments: list[Path], out: Path) -> None:
    """ffmpeg concat в правильном порядке. Все сегменты должны быть в
    одном кодеке (libx264 + aac) — обеспечивается выше."""
    list_file = out.parent / "concat.txt"
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in segments))
    # Сначала пробуем демаксер concat (быстро); если разные кодеки — fallback на re-encode
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(out)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if r.returncode != 0:
        logger.warning(f"concat copy failed, re-encoding: {r.stderr[:200]}")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(list_file),
             "-c:v", "libx264", "-c:a", "aac", "-preset", "fast", "-crf", "20",
             "-pix_fmt", "yuv420p", str(out)],
            check=True, timeout=300,
        )
    try: list_file.unlink()
    except OSError: pass


def execute_hybrid_remake(
    db: Session,
    gv: GeneratedVideo,
) -> str:
    """Сделать полный hybrid remake для уже-созданной GeneratedVideo записи.

    GV должен иметь source_reel_id указывающий на проанализированный Reel
    с media_storage_key + scenes (классифицированными).

    Возвращает R2 storage_key финального видео.
    """
    reel = db.query(Reel).filter(Reel.id == gv.source_reel_id).first()
    if not reel:
        raise RuntimeError("source_reel not found")
    user = db.query(User).filter(User.id == gv.user_id).first()
    if not user or not user.runway_api_key:
        raise RuntimeError("user runway_api_key required")
    if not reel.media_storage_key:
        raise RuntimeError("source reel has no media")

    # Восстановить scenes (с типами если есть)
    try:
        scenes = json.loads(reel.scenes) if reel.scenes else []
    except json.JSONDecodeError:
        scenes = []
    if not scenes:
        raise RuntimeError("reel has no scenes")
    # Проверяем что scenes classified (имеют 'type')
    if not all("type" in s for s in scenes):
        raise RuntimeError("scenes not classified — run scene_classifier first")

    recipe = (db.query(ContentRecipe)
              .filter(ContentRecipe.source_reel_id == reel.id)
              .order_by(ContentRecipe.created_at.desc()).first())
    canonical = (recipe.canonical_prompt if recipe else "") or ""

    # RemakeParams из provider_params.remake_params
    rp = (gv.provider_params or {}).get("remake_params", {}) or {}
    params = RemakeParams(
        brand=rp.get("brand"), product_description=rp.get("product_description"),
        face_description=rp.get("face_description"),
        voice_description=rp.get("voice_description"),
        location_description=rp.get("location_description"),
        outfit_description=rp.get("outfit_description"),
        palette=rp.get("palette"),
        extra_instructions=rp.get("extra_instructions"),
    )
    model = rp.get("model") or (gv.provider_params or {}).get("model") or "gen4.5"

    # Download source from R2
    from app.core.storage import get_r2
    r2 = get_r2()
    workdir = Path(tempfile.mkdtemp(prefix=f"hybrid_{gv.id}_"))
    src = workdir / "src.mp4"
    r2._client.download_file(r2.bucket, reel.media_storage_key, str(src))
    src_dur = _ffprobe_duration(src)
    logger.info(f"hybrid remake gv #{gv.id}: source {src_dur:.1f}s, {len(scenes)} scenes")

    # PR #22 — параллелим Runway-вызовы для regenerate-сегментов
    # ThreadPoolExecutor wall-clock: 6-9 мин serial → 2-3 мин parallel
    # (Runway concurrent limit на gen4.5 = 1 у tier free, у paid выше;
    # тут ограничиваем 4 чтобы не упереться в throttle).
    from concurrent.futures import ThreadPoolExecutor, as_completed
    segments: list[Optional[Path]] = [None] * len(scenes)
    segments_meta: list[Optional[dict]] = [None] * len(scenes)
    runway_jobs: dict = {}  # future → (idx, scene, seg_out)

    def _do_local(i: int, scene: dict, seg_out: Path) -> tuple[int, str, float]:
        """Локальные ffmpeg-стратегии (keep_original, text_template) — быстрые."""
        strategy = scene.get("strategy", "regenerate")
        t0 = time.time()
        if strategy == "keep_original":
            _cut_segment(src, scene["start"], scene["duration"], seg_out)
        elif strategy == "text_template":
            _text_card_video(scene.get("visible_text") or "Magic Forge",
                             scene["duration"], seg_out)
        elif strategy == "image_edit_overlay":
            _cut_segment(src, scene["start"], scene["duration"], seg_out)
            strategy = "keep_original_pending_image_edit"
        else:
            _cut_segment(src, scene["start"], scene["duration"], seg_out)
            strategy = "fallback_keep_original"
        return i, strategy, time.time() - t0

    try:
        # Шаг 1: ffmpeg-сегменты СРАЗУ (быстро), Runway-сегменты собираем для thread pool
        for i, scene in enumerate(scenes):
            strategy = scene.get("strategy", "regenerate")
            seg_out = workdir / f"seg_{i:02d}.mp4"
            stype = scene.get("type", "unknown")
            logger.info(f"  scheduling segment {i+1}/{len(scenes)} "
                        f"({scene['start']:.1f}-{scene['end']:.1f}s) "
                        f"type={stype} strategy={strategy}")
            if strategy == "regenerate":
                runway_jobs[i] = (scene, seg_out)
            else:
                idx, eff_strategy, elapsed = _do_local(i, scene, seg_out)
                segments[idx] = seg_out
                segments_meta[idx] = {
                    "index": idx, "start": scene["start"], "end": scene["end"],
                    "type": stype, "strategy": eff_strategy,
                    "elapsed_sec": round(elapsed, 1),
                }

        # Шаг 2: Runway chunks параллельно (max 4 одновременно — Runway throttle safe)
        if runway_jobs:
            logger.info(f"  launching {len(runway_jobs)} Runway chunks in parallel")
            t_par = time.time()
            with ThreadPoolExecutor(max_workers=min(4, len(runway_jobs))) as ex:
                future_to_idx = {
                    ex.submit(_runway_chunk, src, scene, canonical, params, user, seg_out, model): i
                    for i, (scene, seg_out) in runway_jobs.items()
                }
                for fut in as_completed(future_to_idx):
                    idx = future_to_idx[fut]
                    scene, seg_out = runway_jobs[idx]
                    stype = scene.get("type", "unknown")
                    try:
                        fut.result()
                        eff_strategy = "regenerate"
                    except Exception as e:
                        logger.warning(f"  runway chunk #{idx} failed, fallback: {e}")
                        _cut_segment(src, scene["start"], scene["duration"], seg_out)
                        eff_strategy = "keep_original_fallback"
                    segments[idx] = seg_out
                    segments_meta[idx] = {
                        "index": idx, "start": scene["start"], "end": scene["end"],
                        "type": stype, "strategy": eff_strategy,
                        "elapsed_sec": round(time.time() - t_par, 1),
                    }
            logger.info(f"  Runway chunks done in {time.time() - t_par:.1f}s wall-clock")

        # Drop placeholders
        segments = [s for s in segments if s is not None]
        segments_meta = [m for m in segments_meta if m is not None]

        # Concat
        final = workdir / "final.mp4"
        _concat_segments(segments, final)
        logger.info(f"hybrid concat done: {final.stat().st_size//1024} KB")

        # Cost breakdown (PR #23)
        from app.core.cost_calculator import cost_breakdown, RUNWAY_USD_CENTS_PER_SEC
        chunks_data = []
        for meta in segments_meta:
            if meta["strategy"] == "regenerate":
                dur = meta["end"] - meta["start"]
                chunks_data.append({
                    "model": model,
                    "duration_sec": 10 if dur > 7 else 5,
                })
        breakdown = cost_breakdown(
            analyzer_audio_sec=src_dur,
            analyzer_frames=6,
            scene_classify_count=len(scenes),
            recipe_count=1 if recipe else 0,
            runway_chunks=chunks_data,
        )
        logger.info(f"💰 hybrid gv #{gv.id} cost ≈ {breakdown['total_usd']}")

        # Upload final to R2
        key = f"users/{user.id}/hybrid/{gv.id}_{uuid.uuid4().hex[:8]}.mp4"
        with final.open("rb") as f:
            r2.upload_bytes(key, f.read(), content_type="video/mp4")
        gv.media_storage_key = key
        gv.media_url = r2.get_public_url(key)
        gv.status = GenerationStatus.READY
        gv.completed_at = datetime.utcnow()
        gv.cost_kopecks = breakdown["total"]  # в USD cents
        gv.provider_params = {**(gv.provider_params or {}),
                              "hybrid_segments": segments_meta,
                              "cost_breakdown": breakdown}
        db.commit()
        logger.info(f"✅ hybrid remake gv #{gv.id} READY ({len(segments)} segments)")
        return key
    finally:
        for p in segments + [src]:
            try: p.unlink(missing_ok=True)
            except OSError: pass
        try:
            for f in workdir.glob("*"):
                try: f.unlink()
                except OSError: pass
            workdir.rmdir()
        except OSError: pass
