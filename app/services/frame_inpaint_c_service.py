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
import time
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
                      out_video: Path, total_duration: float,
                      transition_sec: float = 0.5) -> None:
    """Stitch edited PNGs with ffmpeg xfade crossfades (PR-5).

    Each slide is held visible for `per_frame` sec, then crossfades into
    the next over `transition_sec`. final_video_len = N*per_frame -
    (N-1)*transition, so we solve for per_frame to land on total_duration.

    Falls back to hard-cut concat for N=1 or if xfade fails.
    """
    if not edited_pngs:
        raise StrategyCError("no edited frames to stitch")
    n = len(edited_pngs)

    if n == 1:
        # Single image → just loop it for total_duration over the audio.
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-loop", "1", "-t", f"{total_duration:.3f}", "-i", str(edited_pngs[0]),
            "-i", str(audio_src),
            "-map", "0:v:0", "-map", "1:a:0?",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-vf", "fps=30,format=yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest",
            str(out_video),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        if r.returncode != 0 or not out_video.exists():
            raise StrategyCError(f"single-frame stitch rc={r.returncode}: {r.stderr[:300]}")
        return

    # Clamp transition so it doesn't exceed per_frame.
    transition = max(0.1, min(transition_sec, total_duration / (n * 2)))
    per_frame = (total_duration + (n - 1) * transition) / n

    cmd: list[str] = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in edited_pngs:
        cmd += ["-loop", "1", "-t", f"{per_frame:.4f}", "-i", str(p)]
    cmd += ["-i", str(audio_src)]

    # Chain xfades: [0][1]xfade=offset=A[v01]; [v01][2]xfade=offset=B[v12]; ...
    filters = []
    prev_label = "0:v"
    cumulative = per_frame  # end-time of accumulated chain so far
    for i in range(1, n):
        offset = cumulative - transition
        out_label = f"v{i}"
        filters.append(
            f"[{prev_label}][{i}:v]xfade=transition=fade"
            f":duration={transition:.4f}:offset={offset:.4f}[{out_label}]"
        )
        prev_label = out_label
        cumulative += per_frame - transition

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", f"[{prev_label}]",
        "-map", f"{n}:a:0?",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-vf", "fps=30,format=yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        str(out_video),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if r.returncode != 0 or not out_video.exists():
        raise StrategyCError(f"xfade stitch rc={r.returncode}: {r.stderr[:400]}")


def _runway_pair_to_segment(
    api_key: str,
    start_url: str,
    end_url: str,  # kept for API compat but not used in this build
    duration_sec: int,
    prompt: str = "",
    *,
    poll_interval: int = 8,
    max_wait_sec: int = 240,
) -> str:
    """Submit one Runway image_to_video segment from a single start keyframe.

    Originally tried promptImage=[{first},{last}] (gen4 spec) but
    gen4_turbo rejects array form with zod validation error. Until we
    integrate Seedance 2.0 / HappyHorse 1.0 (which support keyframe
    control natively in Runway API) we send only the start frame as a
    string URL. The caller stitches consecutive segments with an ffmpeg
    crossfade to hide the discontinuity at segment boundaries.

    Runway accepts only duration ∈ {5, 10} for gen4_turbo.

    Raises StrategyCError on any failure.
    """
    import requests

    api_duration = 5 if duration_sec <= 5 else 10
    payload = {
        "model": "gen4_turbo",
        "promptText": (prompt or "smooth natural motion, preserve composition")[:1000],
        "ratio": "720:1280",
        "duration": api_duration,
        "promptImage": start_url,  # string URL form — single start keyframe
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Runway-Version": "2024-11-06",
        "Content-Type": "application/json",
    }
    submit_url = "https://api.dev.runwayml.com/v1/image_to_video"
    r = requests.post(submit_url, headers=headers, json=payload, timeout=60)
    if r.status_code >= 400:
        body_text = r.text or ""
        low = body_text.lower()
        if "not enough credits" in low or "insufficient" in low or r.status_code == 402:
            raise StrategyCError(
                "💳 Закончились Runway-кредиты. Пополни на "
                "https://dev.runwayml.com/billing и попробуй ещё раз. "
                "Один тест C+ при N=5 = ~$1.00."
            )
        if "moderat" in low:
            raise StrategyCError(
                "🛑 Runway moderation отбил кадр (наверняка лицо/откровенный "
                "контент). Попробуй другой ролик или уменьши c_keyframe_count."
            )
        if r.status_code in (401, 403):
            raise StrategyCError("🔑 Runway API-key недействителен — проверь в профиле.")
        raise StrategyCError(f"Runway submit HTTP {r.status_code}: {body_text[:300]}")
    body = r.json()
    task_id = body.get("id")
    if not task_id:
        raise StrategyCError(f"Runway submit: no task id in {body}")

    # poll
    poll_url = f"https://api.dev.runwayml.com/v1/tasks/{task_id}"
    elapsed = 0
    while elapsed < max_wait_sec:
        time.sleep(poll_interval)
        elapsed += poll_interval
        rr = requests.get(poll_url, headers=headers, timeout=60)
        if rr.status_code >= 400:
            raise StrategyCError(f"Runway poll HTTP {rr.status_code}: {rr.text[:300]}")
        b = rr.json()
        status = b.get("status", "")
        if status == "SUCCEEDED":
            output = b.get("output") or []
            if isinstance(output, list) and output:
                return output[0]
            if isinstance(output, str):
                return output
            raise StrategyCError(f"Runway SUCCEEDED but no output: {b}")
        if status in ("FAILED", "CANCELLED"):
            raise StrategyCError(f"Runway task {status}: {b.get('failure') or b}")
    raise StrategyCError(f"Runway poll timeout after {max_wait_sec}s")


def _download_to(url: str, dst: Path, timeout: int = 120) -> None:
    import requests
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with dst.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def _smooth_via_runway(
    edited_pngs: list[Path],
    audio_src: Path,
    out_video: Path,
    total_duration: float,
    *,
    user_id: int,
    runway_api_key: str,
    runway_prompt: str,
    r2,
    workdir: Path,
) -> int:
    """C+ mode: between every pair of consecutive edited keyframes, ask
    Runway image_to_video to interpolate. Returns count of Runway segments
    actually used (caller multiplies by per-second pricing for cost).
    """
    n = len(edited_pngs)
    if n < 2:
        raise StrategyCError("need ≥2 keyframes for smooth mode")

    # Upload all edited PNGs to R2 so Runway can fetch them.
    kf_urls: list[str] = []
    kf_keys: list[str] = []
    for i, p in enumerate(edited_pngs):
        key = f"users/{user_id}/forge_c_tmp/{uuid.uuid4().hex[:8]}_kf{i}.png"
        with p.open("rb") as f:
            r2.upload_bytes(key, f.read(), content_type="image/png")
        kf_urls.append(r2.get_public_url(key))
        kf_keys.append(key)

    try:
        per_segment_sec = max(1.0, total_duration / (n - 1))
        # Runway clamps to 5 or 10 sec — pick smaller bucket if our gap is short.
        segments_out: list[Path] = []
        for i in range(n - 1):
            seg_path = workdir / f"runway_seg_{i:02d}.mp4"
            logger.info(f"  runway segment {i+1}/{n-1} "
                        f"(target {per_segment_sec:.1f}s)")
            seg_url = _runway_pair_to_segment(
                runway_api_key,
                start_url=kf_urls[i],
                end_url=kf_urls[i + 1],
                duration_sec=int(per_segment_sec) if per_segment_sec >= 5 else 5,
                prompt=runway_prompt,
            )
            _download_to(seg_url, seg_path)
            # trim to per_segment_sec if Runway gave us a longer 5s/10s clip
            trimmed = workdir / f"runway_seg_{i:02d}_trim.mp4"
            tr = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-i", str(seg_path), "-t", f"{per_segment_sec:.3f}",
                 "-c:v", "libx264", "-an", str(trimmed)],
                capture_output=True, text=True, timeout=120, check=False,
            )
            if tr.returncode != 0:
                # fall back to untrimmed if trim fails
                shutil.copy(seg_path, trimmed)
            segments_out.append(trimmed)

        # Stitch segments with xfade to soften the start-frame-only jumps
        # between segments. (When we upgrade to real keyframe control we
        # can drop the xfade since segments will land on the next kf.)
        if len(segments_out) == 1:
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(segments_out[0]),
                "-i", str(audio_src),
                "-map", "0:v:0", "-map", "1:a:0?",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-shortest",
                str(out_video),
            ]
        else:
            transition = 0.4
            cmd = ["ffmpeg", "-y", "-loglevel", "error"]
            for seg in segments_out:
                cmd += ["-i", str(seg)]
            cmd += ["-i", str(audio_src)]
            filters = []
            prev = "0:v"
            cumulative = per_segment_sec
            for i in range(1, len(segments_out)):
                offset = cumulative - transition
                out_label = f"v{i}"
                filters.append(
                    f"[{prev}][{i}:v]xfade=transition=fade"
                    f":duration={transition:.4f}:offset={offset:.4f}[{out_label}]"
                )
                prev = out_label
                cumulative += per_segment_sec - transition
            cmd += [
                "-filter_complex", ";".join(filters),
                "-map", f"[{prev}]",
                "-map", f"{len(segments_out)}:a:0?",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-shortest",
                str(out_video),
            ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        if r.returncode != 0 or not out_video.exists():
            raise StrategyCError(f"runway-mode stitch rc={r.returncode}: {r.stderr[:400]}")
        return n - 1
    finally:
        # Best-effort cleanup of temp R2 keyframe uploads
        for k in kf_keys:
            try:
                r2.delete(k)
            except Exception:
                pass


def run_strategy_c(
    db: Session,
    user: User,
    *,
    source_url: str,
    brand: Optional[str] = None,
    product_description: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    keyframe_count: int = 5,
    smooth_transitions: bool = False,
) -> dict:
    """End-to-end. Sync; expect 2-5 min for N=5, 4-8 min for N=10.

    If `smooth_transitions=True`, between each pair of edited keyframes
    Runway image_to_video interpolates a real motion clip (~$0.25 per
    segment for gen4_turbo @ 5s × $0.05/s). Total cost for N=5 jumps
    from ~$0.21 to ~$1.21. Wall time ~3-5 extra min for the Runway polls.
    """
    if not user.openai_api_key:
        raise StrategyCError("Strategy C нужен openai_api_key в профиле")
    if smooth_transitions and not user.runway_api_key:
        raise StrategyCError(
            "C+ smooth_transitions нужен runway_api_key в профиле "
            "(сними галочку 🎬 если не хочешь использовать Runway)"
        )
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

        # 4. stitch
        runway_segments = 0
        if smooth_transitions and len(edited_pngs) >= 2:
            runway_prompt = (
                "smooth natural motion, same subject and composition as "
                "start and end frames, photorealistic"
            )
            runway_segments = _smooth_via_runway(
                edited_pngs, src_mp4, final_mp4, duration,
                user_id=user.id,
                runway_api_key=user.runway_api_key,
                runway_prompt=runway_prompt,
                r2=r2, workdir=workdir,
            )
        else:
            _stitch_slideshow(edited_pngs, src_mp4, final_mp4, duration)

        # 5. R2 upload
        key = f"users/{user.id}/forge_c/{uuid.uuid4().hex[:12]}.mp4"
        with final_mp4.open("rb") as f:
            r2.upload_bytes(key, f.read(), content_type="video/mp4")
        media_url = r2.get_public_url(key)

        # 6. persist + cost calc
        cost_usd = 0.04 * len(edited_pngs) + 0.01  # gpt-image-1 medium + overhead
        if runway_segments:
            # gen4_turbo @ $0.05/sec, each segment is 5 or 10 sec
            per_segment_sec = max(1.0, duration / (len(edited_pngs) - 1))
            api_sec = 5 if per_segment_sec < 5 else 10
            cost_usd += runway_segments * api_sec * 0.05
        prompt_tag = f"strategy=C N={kf_count}"
        if smooth_transitions:
            prompt_tag += " +runway"
        gv = GeneratedVideo(
            user_id=user.id,
            provider=VideoProvider.RUNWAY if smooth_transitions else VideoProvider.MOCK,
            status=GenerationStatus.READY,
            prompt=f"[{prompt_tag}] source={source_url}",
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
