"""
Strategy B — Uniqualizer pipeline.

Takes a competitor reel URL → produces a perceptually-distinct copy:
  1. yt-dlp download original
  2. ffmpeg uniqify preset (hue/zoom/rotation/speed + audio pitch+atempo)
  3. (optional) Whisper transcribe + LLM rewrite + .ass burn-in
  4. upload to R2 → return public URL

Cost ~$0.005-0.015 per video. Synchronous, ~30-90 sec per reel.

This is NOT a "new video" — it's the same content perturbed to defeat
platform fingerprinting (pHash, audio fp, OCR text match). Use Strategy A
or C when you need a genuinely-new remake.
"""
from __future__ import annotations

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
from app.core.uniqualizer import uniqify_video, UniqifyPreset, UniqifyError
from app.core.yt_downloader import download_video, DownloadError
from app.core.analyzers.transcriber import transcribe_audio_segments, TranscribeError

logger = logging.getLogger(__name__)


class StrategyBError(Exception):
    pass


def _rewrite_segments_llm(
    segments: list[dict],
    *,
    openai_api_key: str,
    brand: Optional[str] = None,
    product_description: Optional[str] = None,
    timeout: int = 60,
) -> list[dict]:
    """Ask LLM to paraphrase each segment, preserving meaning + length.

    Returns the same list with `.text` replaced. On any error, returns
    the originals unchanged (subtitle rewrite is best-effort).
    """
    if not segments:
        return segments
    try:
        from openai import OpenAI
    except ImportError as e:
        logger.warning(f"openai SDK missing, skip subtitle rewrite: {e}")
        return segments
    client = OpenAI(api_key=openai_api_key, timeout=timeout)
    src = [{"i": i, "t": s["text"]} for i, s in enumerate(segments)]
    sys = (
        "Paraphrase each subtitle segment. Same meaning, same approximate "
        "length (±20%), same tone, same language. Output STRICT JSON: "
        '{"items":[{"i":0,"t":"..."},...]} — no fences, no commentary.'
    )
    if brand:
        sys += f" If a brand name appears, replace it with '{brand}'."
    if product_description:
        sys += f" If a product is described, swap for: {product_description[:200]}."
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": json.dumps(src, ensure_ascii=False)}],
            response_format={"type": "json_object"},
            max_tokens=2000, temperature=0.7,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        items = data.get("items") or []
        by_i = {int(it.get("i", -1)): (it.get("t") or "").strip() for it in items}
        out = []
        for i, s in enumerate(segments):
            nt = by_i.get(i)
            out.append({**s, "text": nt if nt else s["text"]})
        return out
    except Exception as e:
        logger.warning(f"LLM rewrite failed, keeping originals: {e}")
        return segments


def _segments_to_ass(segments: list[dict], font_size: int = 56) -> str:
    """Build a minimal Advanced SubStation Alpha subtitle file.

    Picked .ass over .srt because ffmpeg's `subtitles` filter supports
    per-style overrides (font, outline) without external fonts dir.
    """
    def fmt(t: float) -> str:
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Inter,{font_size},&H00FFFFFF,&H00000000,&H66000000,"
        f"1,0,1,3,1,2,80,80,180,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [header]
    for s in segments:
        text = (s.get("text") or "").replace("\n", " ").replace("{", "(").replace("}", ")")
        if not text:
            continue
        lines.append(f"Dialogue: 0,{fmt(s['start'])},{fmt(s['end'])},Default,,0,0,0,,{text}\n")
    return "".join(lines)


def _burn_subs(src_video: Path, ass_path: Path, dst_video: Path, timeout: int = 300) -> None:
    if shutil.which("ffmpeg") is None:
        raise StrategyBError("ffmpeg not in PATH")
    # ffmpeg `subtitles` filter wants escaped path with quoted colon on Linux
    ass_escaped = str(ass_path).replace(":", r"\:").replace("'", r"\'")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src_video),
        "-vf", f"subtitles='{ass_escaped}'",
        "-c:v", "libx264", "-crf", "22", "-preset", "fast",
        "-c:a", "copy",
        str(dst_video),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if r.returncode != 0:
        raise StrategyBError(f"burn-in ffmpeg rc={r.returncode}: {r.stderr[:400]}")


def run_strategy_b(
    db: Session,
    user: User,
    *,
    source_url: str,
    mutate_video: bool = True,
    mutate_audio: bool = True,
    rewrite_subs: bool = True,
    brand: Optional[str] = None,
    product_description: Optional[str] = None,
) -> dict:
    """End-to-end pipeline. Sync; expect 30-90 sec wall time."""
    if rewrite_subs and not user.openai_api_key:
        raise StrategyBError(
            "Перезапись субтитров требует openai_api_key в профиле "
            "(или сними галочку 📝 в форме)."
        )
    try:
        from app.core.storage import get_r2
        r2 = get_r2()
    except Exception as e:
        raise StrategyBError(f"R2 not available: {e}")

    workdir = Path(tempfile.mkdtemp(prefix="strat_b_"))
    src_mp4 = workdir / "source.mp4"
    uniq_mp4 = workdir / "uniq.mp4"
    final_mp4 = workdir / "final.mp4"
    ass_path = workdir / "subs.ass"
    try:
        # 1. download
        logger.info(f"strategy B: download {source_url}")
        try:
            downloaded, meta = download_video(source_url, out_dir=workdir)
        except DownloadError as e:
            raise StrategyBError(f"download failed: {e}")
        if downloaded != src_mp4:
            shutil.copy(downloaded, src_mp4)

        # 2. uniqify (conditionally skip video/audio mutations)
        preset = UniqifyPreset()
        if not mutate_video:
            # zero-out visual params
            preset.hue_shift = 0
            preset.saturation = 1.0
            preset.brightness = 0.0
            preset.contrast = 1.0
            preset.scale_factor = 1.0
            preset.rotate_deg = 0.0
            preset.speed_factor = 1.0
            preset.noise_strength = 0
        if not mutate_audio:
            preset.audio_pitch_semitones = 0.0
        try:
            uniqify_video(src_mp4, uniq_mp4, preset=preset, randomise=mutate_video)
        except UniqifyError as e:
            raise StrategyBError(f"uniqify failed: {e}")

        # 3. optional subtitle rewrite + burn-in
        if rewrite_subs:
            try:
                segments = transcribe_audio_segments(uniq_mp4, user.openai_api_key)
            except TranscribeError as e:
                logger.warning(f"transcribe failed, skipping subs: {e}")
                segments = []
            if segments:
                segments = _rewrite_segments_llm(
                    segments,
                    openai_api_key=user.openai_api_key,
                    brand=brand,
                    product_description=product_description,
                )
                ass_path.write_text(_segments_to_ass(segments), encoding="utf-8")
                try:
                    _burn_subs(uniq_mp4, ass_path, final_mp4)
                except StrategyBError as e:
                    logger.warning(f"burn-in failed, using uniq w/o subs: {e}")
                    shutil.copy(uniq_mp4, final_mp4)
            else:
                shutil.copy(uniq_mp4, final_mp4)
        else:
            shutil.copy(uniq_mp4, final_mp4)

        # 4. upload to R2
        key = f"users/{user.id}/forge_b/{uuid.uuid4().hex[:12]}.mp4"
        with final_mp4.open("rb") as f:
            r2.upload_bytes(key, f.read(), content_type="video/mp4")
        media_url = r2.get_public_url(key)

        # 5. persist as GeneratedVideo
        cost_usd = 0.005  # uniqify CPU only
        if rewrite_subs:
            cost_usd += 0.01  # Whisper ($0.006/min ≈ $0.003 for a 30s reel) + LLM ($0.005)
        gv = GeneratedVideo(
            user_id=user.id,
            provider=VideoProvider.MOCK,  # uniqualizer = local ffmpeg, no external provider
            status=GenerationStatus.READY,
            prompt=f"[strategy=B uniqualizer] source={source_url}",
            media_url=media_url,
            media_storage_key=key,
            completed_at=datetime.utcnow(),
        )
        db.add(gv)
        db.commit()
        db.refresh(gv)
        logger.info(f"✅ strategy B done: gv #{gv.id} → {media_url}")

        return {
            "gv_id": gv.id,
            "media_url": media_url,
            "source_title": (meta or {}).get("title"),
            "cost_usd": cost_usd,
        }
    finally:
        for p in (src_mp4, uniq_mp4, final_mp4, ass_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass
