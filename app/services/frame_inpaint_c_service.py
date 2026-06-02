"""
Strategy C — Frame-level inpaint MVP.

Pipeline:
  1. yt-dlp download original
  2. ffprobe duration
  3. ffmpeg extract N keyframes (PNG, 1024x1536 for 9:16)
  4. for each keyframe: gpt-image-1 images.edit with substitution prompt
  5. ffmpeg stitch edited PNGs as a slideshow (each held for duration/N
     seconds) and overlay the original audio track
  6. R2 upload → return media_url

MVP compromise: no inter-frame interpolation, so the output looks like
N discrete slides rather than continuous motion. The win: subtitle
timing is exact (each slide aligned to the original timeline). v2 will
add FILM/RIFE between slides for smooth motion.

Cost: $0.04 per gpt-image-1 medium-quality edit × N keyframes,
+ ~$0.005 OpenAI overhead. N=5 → ~$0.20.
"""
from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.generation import GeneratedVideo, VideoProvider, GenerationStatus
from app.core.yt_downloader import download_video, DownloadError

logger = logging.getLogger(__name__)


class StrategyCError(Exception):
    pass


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if r.returncode != 0:
        raise StrategyCError(f"ffprobe failed: {r.stderr[:200]}")
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise StrategyCError(f"ffprobe non-numeric: {r.stdout[:100]}")


def _extract_keyframe(src: Path, ts: float, dst: Path) -> None:
    # 1024x1536 = 2:3 = portrait 9:16-ish, what gpt-image-1 accepts.
    # We scale source maintaining aspect ratio then pad to exact size.
    vf = (
        "scale=1024:1536:force_original_aspect_ratio=decrease,"
        "pad=1024:1536:(ow-iw)/2:(oh-ih)/2:black"
    )
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", f"{ts:.3f}", "-i", str(src),
         "-frames:v", "1", "-vf", vf, "-q:v", "2",
         str(dst)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if r.returncode != 0 or not dst.exists():
        raise StrategyCError(f"keyframe @ {ts}s: ffmpeg rc={r.returncode}: {r.stderr[:200]}")


def _build_inpaint_prompt(brand: Optional[str], product: Optional[str],
                          face: Optional[str], outfit: Optional[str],
                          extra: Optional[str]) -> str:
    parts = [
        "Re-render this exact scene preserving composition, camera angle, "
        "lighting, pose, and body position, but apply the following swaps:",
    ]
    if brand:
        parts.append(f"- visible brand/logo → '{brand}'")
    if product:
        parts.append(f"- product in hand or focus → {product}")
    if face:
        parts.append(f"- person's face → {face}")
    if outfit:
        parts.append(f"- clothing → {outfit}")
    if extra:
        parts.append(f"- additional: {extra}")
    if len(parts) == 1:
        parts.append("- subtle uniqification: shift palette warmer, "
                     "swap any visible brand for a generic placeholder")
    parts.append(
        "Keep facial expression, gesture, and on-screen subtitle text "
        "EXACTLY as in the source. Output 1024x1536 portrait, photorealistic."
    )
    return "\n".join(parts)


def _edit_keyframe_via_gpt_image(
    png_path: Path,
    prompt: str,
    *,
    openai_api_key: str,
    out_path: Path,
    timeout: int = 120,
) -> None:
    """Call gpt-image-1 images.edit with the PNG as input, write result to out_path.

    Falls back gracefully — on any API error the original is copied through,
    so a partial failure doesn't kill the whole pipeline.
    """
    try:
        from openai import OpenAI
    except ImportError as e:
        raise StrategyCError(f"openai SDK: {e}")
    client = OpenAI(api_key=openai_api_key, timeout=timeout)
    try:
        with png_path.open("rb") as f:
            resp = client.images.edit(
                model="gpt-image-1",
                image=f,
                prompt=prompt[:1000],  # gpt-image-1 prompt cap is ~1000 chars
                size="1024x1536",
                n=1,
            )
        b64 = resp.data[0].b64_json
        if not b64:
            raise StrategyCError("gpt-image-1 returned empty b64_json")
        out_path.write_bytes(base64.b64decode(b64))
    except Exception as e:
        logger.warning(f"gpt-image-1 edit failed ({type(e).__name__}: {str(e)[:120]}), "
                       f"using original frame")
        shutil.copy(png_path, out_path)


def _stitch_slideshow(edited_pngs: list[Path], audio_src: Path,
                      out_video: Path, total_duration: float) -> None:
    if not edited_pngs:
        raise StrategyCError("no edited frames to stitch")
    per_frame = total_duration / len(edited_pngs)
    # Build a concat-demuxer file: each png held `per_frame` sec.
    concat = out_video.parent / "concat.txt"
    lines = []
    for p in edited_pngs:
        lines.append(f"file '{p.resolve()}'")
        lines.append(f"duration {per_frame:.4f}")
    # ffmpeg concat needs the final file repeated without `duration`
    lines.append(f"file '{edited_pngs[-1].resolve()}'")
    concat.write_text("\n".join(lines), encoding="utf-8")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat),
        "-i", str(audio_src),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-vf", "fps=30,format=yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(out_video),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if r.returncode != 0 or not out_video.exists():
        raise StrategyCError(f"stitch ffmpeg rc={r.returncode}: {r.stderr[:300]}")


def run_strategy_c(
    db: Session,
    user: User,
    *,
    source_url: str,
    brand: Optional[str] = None,
    product_description: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    keyframe_count: int = 5,
) -> dict:
    """End-to-end. Sync; expect 2-5 min for N=5, 4-8 min for N=10."""
    if not user.openai_api_key:
        raise StrategyCError("Strategy C нужен openai_api_key в профиле")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise StrategyCError("ffmpeg/ffprobe not in PATH")

    # Parse face/outfit out of extra_instructions ("face: ... | outfit: ...")
    face = outfit = extra = None
    if extra_instructions:
        chunks = [c.strip() for c in extra_instructions.split("|")]
        rest = []
        for c in chunks:
            low = c.lower()
            if low.startswith("face:"):
                face = c[5:].strip()
            elif low.startswith("outfit:"):
                outfit = c[7:].strip()
            else:
                rest.append(c)
        if rest:
            extra = " | ".join(rest)

    try:
        from app.core.storage import get_r2
        r2 = get_r2()
    except Exception as e:
        raise StrategyCError(f"R2 not available: {e}")

    workdir = Path(tempfile.mkdtemp(prefix="strat_c_"))
    src_mp4 = workdir / "source.mp4"
    final_mp4 = workdir / "final.mp4"
    try:
        # 1. download
        logger.info(f"strategy C: download {source_url}")
        try:
            downloaded, meta = download_video(source_url, out_dir=workdir)
        except DownloadError as e:
            raise StrategyCError(f"download failed: {e}")
        if downloaded != src_mp4:
            shutil.copy(downloaded, src_mp4)

        duration = _probe_duration(src_mp4)
        if duration < 1.0:
            raise StrategyCError(f"source too short ({duration:.2f}s)")

        # 2. extract N keyframes evenly across [0.5s, duration-0.5s]
        kf_count = max(3, min(int(keyframe_count or 5), 30))
        margin = min(0.5, duration * 0.1)
        timestamps = [margin + i * (duration - 2 * margin) / max(1, kf_count - 1)
                      for i in range(kf_count)]
        src_pngs: list[Path] = []
        for i, ts in enumerate(timestamps):
            p = workdir / f"kf_{i:02d}_src.png"
            _extract_keyframe(src_mp4, ts, p)
            src_pngs.append(p)
        logger.info(f"strategy C: extracted {len(src_pngs)} keyframes")

        # 3. inpaint each via gpt-image-1
        prompt = _build_inpaint_prompt(brand, product_description, face, outfit, extra)
        edited_pngs: list[Path] = []
        for i, src_png in enumerate(src_pngs):
            out_png = workdir / f"kf_{i:02d}_edit.png"
            _edit_keyframe_via_gpt_image(
                src_png, prompt, openai_api_key=user.openai_api_key, out_path=out_png,
            )
            edited_pngs.append(out_png)
            logger.info(f"  kf {i+1}/{len(src_pngs)} edited")

        # 4. stitch slideshow + original audio
        _stitch_slideshow(edited_pngs, src_mp4, final_mp4, duration)

        # 5. R2 upload
        key = f"users/{user.id}/forge_c/{uuid.uuid4().hex[:12]}.mp4"
        with final_mp4.open("rb") as f:
            r2.upload_bytes(key, f.read(), content_type="video/mp4")
        media_url = r2.get_public_url(key)

        # 6. persist
        cost_usd = 0.04 * len(edited_pngs) + 0.01  # gpt-image-1 medium + overhead
        gv = GeneratedVideo(
            user_id=user.id,
            provider=VideoProvider.MOCK,  # local-stitched, no external video provider
            status=GenerationStatus.READY,
            prompt=f"[strategy=C frame-inpaint N={kf_count}] source={source_url}",
            media_url=media_url,
            media_storage_key=key,
            completed_at=datetime.utcnow(),
        )
        db.add(gv)
        db.commit()
        db.refresh(gv)
        logger.info(f"✅ strategy C done: gv #{gv.id} → {media_url}")

        return {
            "gv_id": gv.id,
            "media_url": media_url,
            "source_title": (meta or {}).get("title"),
            "cost_usd": round(cost_usd, 3),
            "keyframes": len(edited_pngs),
        }
    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except OSError:
            pass
