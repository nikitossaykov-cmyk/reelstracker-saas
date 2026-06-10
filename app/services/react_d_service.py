"""
Strategy D — React / Reply-to-trend.

Pipeline:
  1. yt-dlp download original
  2. ffmpeg cut first 5 sec (the "trend hook" piece)
  3. Whisper-transcribe whole original (for context)
  4. LLM (gpt-4o-mini) generates a 15-sec blogger-style reply
     mentioning brand + product
  5. OpenAI tts-1 synthesizes that reply as audio
  6. gpt-image-1 generates 2 keyframes of an "AI blogger" with the
     product (per brand/product/face params)
  7. ffmpeg builds the AI-segment: keyframes as xfade slideshow,
     overlay subtitled .ass, mix tts audio
  8. ffmpeg concat: [first_5s_hook] -> [bridge caption "А ВОТ Я ДУМАЮ"]
     -> [AI segment]
  9. R2 upload

Cost ~$0.08-0.12: gpt-image-1 ×2 ($0.08) + Whisper ($0.003) +
LLM gen ($0.005) + tts-1 ($0.015 per ~150 words) + R2 (free).
Wall clock: ~60-120 sec.
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
from app.core.analyzers.transcriber import transcribe_audio, TranscribeError

logger = logging.getLogger(__name__)


class StrategyDError(Exception):
    pass


# ─── ffmpeg helpers ─────────────────────────────────────

def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if r.returncode != 0:
        raise StrategyDError(f"ffprobe failed: {r.stderr[:200]}")
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise StrategyDError(f"ffprobe non-numeric: {r.stdout[:100]}")


def _cut(src: Path, start: float, length: float, dst: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
        "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,"
               "pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,fps=30,settb=AVTB,format=yuv420p",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if r.returncode != 0 or not dst.exists():
        raise StrategyDError(f"cut ffmpeg rc={r.returncode}: {r.stderr[:300]}")


# ─── LLM: generate blogger reply ────────────────────────

REPLY_SYSTEM = """\
You write a short, casual blogger-style spoken voice-over for a TikTok/Reels
react video. The original video is shown for 5 sec first; then your text
plays as voiceover while we show product shots.

Output STRICT JSON:
{
  "reply_text": "natural spoken phrase, 30-40 words, 10-15 sec when read aloud",
  "bridge_caption": "1-3 word transition caption shown between the two parts (e.g. 'А вот я думаю...')"
}

Tone: conversational, first person, NO hype emoji bombs, like a real person
sharing genuine reaction. Mention the brand and product naturally once.
Language: same as the original transcript (default Russian if unclear).
"""


def _build_reply(
    *,
    openai_api_key: str,
    original_transcript: str,
    brand: Optional[str],
    product_description: Optional[str],
    extra_instructions: Optional[str],
    timeout: int = 60,
) -> dict:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise StrategyDError(f"openai SDK: {e}")
    client = OpenAI(api_key=openai_api_key, timeout=timeout)
    parts = [f"Original transcript: {original_transcript[:1500]}"]
    if brand:
        parts.append(f"Brand to mention: {brand}")
    if product_description:
        parts.append(f"Product: {product_description}")
    if extra_instructions:
        parts.append(f"Extra: {extra_instructions}")
    user_msg = "\n".join(parts)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": REPLY_SYSTEM},
                      {"role": "user", "content": user_msg}],
            response_format={"type": "json_object"},
            max_tokens=400, temperature=0.7,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        raise StrategyDError(f"LLM reply: {type(e).__name__}: {str(e)[:200]}")
    if not data.get("reply_text"):
        raise StrategyDError("LLM returned empty reply_text")
    data.setdefault("bridge_caption", "А ВОТ Я ДУМАЮ…")
    return data


# ─── TTS ────────────────────────────────────────────────

def _synthesize_tts(text: str, *, openai_api_key: str, out_path: Path,
                    voice: str = "nova", timeout: int = 60) -> float:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise StrategyDError(f"openai SDK: {e}")
    client = OpenAI(api_key=openai_api_key, timeout=timeout)
    try:
        resp = client.audio.speech.create(model="tts-1", voice=voice, input=text)
        resp.stream_to_file(str(out_path))
    except Exception as e:
        raise StrategyDError(f"tts: {type(e).__name__}: {str(e)[:200]}")
    return _probe_duration(out_path)


# ─── gpt-image-1 keyframes (reuse pattern from C) ───────

def _gen_keyframe(prompt: str, openai_api_key: str, out_path: Path,
                  timeout: int = 120) -> None:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise StrategyDError(f"openai SDK: {e}")
    client = OpenAI(api_key=openai_api_key, timeout=timeout)
    try:
        resp = client.images.generate(
            model="gpt-image-1",
            prompt=prompt[:1000],
            size="1024x1536",
            n=1,
        )
        b64 = resp.data[0].b64_json
        if not b64:
            raise StrategyDError("gpt-image-1 empty b64_json")
        out_path.write_bytes(base64.b64decode(b64))
    except Exception as e:
        raise StrategyDError(f"gpt-image-1: {type(e).__name__}: {str(e)[:200]}")


# ─── AI-segment assembly ────────────────────────────────

def _ass_for_text(text: str, duration: float, font_size: int = 56) -> str:
    """Generate .ass subtitle with full text spread over duration."""
    def fmt(t: float) -> str:
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    # split into ~6-word chunks
    words = text.split()
    chunks = [" ".join(words[i:i+6]) for i in range(0, len(words), 6)] or [text]
    per_chunk = duration / len(chunks)

    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 720\nPlayResY: 1280\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Inter,{font_size},&H00FFFFFF,&H00000000,&H99000000,"
        f"1,0,1,3,1,2,40,40,120,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    out = [header]
    t = 0.0
    for ch in chunks:
        out.append(f"Dialogue: 0,{fmt(t)},{fmt(t+per_chunk)},Default,,0,0,0,,{ch}\n")
        t += per_chunk
    return "".join(out)


def _build_ai_segment(
    kf_paths: list[Path],
    tts_path: Path,
    duration: float,
    ass_path: Path,
    out_path: Path,
    workdir: Path,
) -> None:
    """xfade slideshow of N keyframes + tts audio + .ass burn-in."""
    if not kf_paths:
        raise StrategyDError("no keyframes for ai segment")

    transition = 0.5
    n = len(kf_paths)
    per_frame = (duration + (n - 1) * transition) / n if n > 1 else duration

    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in kf_paths:
        cmd += ["-loop", "1", "-t", f"{per_frame:.4f}", "-i", str(p)]
    cmd += ["-i", str(tts_path)]

    filters = []
    for i in range(n):
        filters.append(
            f"[{i}:v]scale=720:1280:force_original_aspect_ratio=decrease,"
            f"pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,fps=30,settb=AVTB,format=yuv420p[v{i}n]"
        )
    if n == 1:
        prev = "v0n"
    else:
        prev = "v0n"
        cumulative = per_frame
        for i in range(1, n):
            offset = cumulative - transition
            out_label = f"x{i}"
            filters.append(
                f"[{prev}][v{i}n]xfade=transition=fade:duration={transition:.3f}:"
                f"offset={offset:.3f}[{out_label}]"
            )
            prev = out_label
            cumulative += per_frame - transition

    # subtitle burn-in
    ass_escaped = str(ass_path).replace(":", r"\:").replace("'", r"\'")
    filters.append(f"[{prev}]subtitles='{ass_escaped}'[final]")

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[final]",
        "-map", f"{n}:a:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if r.returncode != 0 or not out_path.exists():
        raise StrategyDError(f"ai-segment ffmpeg rc={r.returncode}: {r.stderr[:400]}")


def _bridge_card(caption: str, duration: float, out_path: Path) -> None:
    """1-2 sec card with bridge text on dark background."""
    safe = caption.replace("'", "")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-t", f"{duration:.3f}",
        "-i", f"color=c=0x0a0a0a:s=720x1280:r=30",
        "-vf", f"drawtext=text='{safe}':fontcolor=white:fontsize=72:"
               f"x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=0x000000@0.5:boxborderw=20",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
        str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if r.returncode != 0 or not out_path.exists():
        raise StrategyDError(f"bridge ffmpeg rc={r.returncode}: {r.stderr[:300]}")


def _concat_three(parts: list[Path], out: Path) -> None:
    """Concat N parts (any of them silent — we synthesize a matching
    silent audio track for bridge). ffmpeg concat filter expects
    interleaved [v0][a0][v1][a1]... inputs, NOT separate v/a chains.
    """
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    # First N inputs are the videos themselves
    for p in parts:
        cmd += ["-i", str(p)]
    # Add a silent stereo audio source for the bridge (input index = N).
    bridge_idx = 1  # in our 3-part flow [hook, bridge, ai] bridge is index 1
    bridge_dur = _probe_duration(parts[bridge_idx])
    cmd += ["-f", "lavfi", "-t", f"{bridge_dur:.3f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    silent_idx = len(parts)

    filters = []
    for i, p in enumerate(parts):
        filters.append(
            f"[{i}:v]scale=720:1280:force_original_aspect_ratio=decrease,"
            f"pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,fps=30,settb=AVTB,format=yuv420p[v{i}]"
        )
    # Audio normalization: real sources via anull; bridge uses silent source.
    for i in range(len(parts)):
        if i == bridge_idx:
            filters.append(
                f"[{silent_idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS[a{i}]"
            )
        else:
            filters.append(
                f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS[a{i}]"
            )
    # Concat filter wants interleaved [v0][a0][v1][a1]…[vN][aN]
    interleaved = "".join(f"[v{i}][a{i}]" for i in range(len(parts)))
    filters.append(
        f"{interleaved}concat=n={len(parts)}:v=1:a=1[vout][aout]"
    )

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if r.returncode != 0 or not out.exists():
        raise StrategyDError(f"concat ffmpeg rc={r.returncode}: {r.stderr[:500]}")


# ─── Main entry ─────────────────────────────────────────

def run_strategy_d(
    db: Session,
    user: User,
    *,
    source_url: str,
    brand: Optional[str] = None,
    product_description: Optional[str] = None,
    face_description: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    hook_seconds: float = 5.0,
    tts_voice: str = "nova",
) -> dict:
    if not user.openai_api_key:
        raise StrategyDError("Strategy D нужен openai_api_key в профиле")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise StrategyDError("ffmpeg/ffprobe not in PATH")

    try:
        from app.core.storage import get_r2
        r2 = get_r2()
    except Exception as e:
        raise StrategyDError(f"R2 not available: {e}")

    workdir = Path(tempfile.mkdtemp(prefix="strat_d_"))
    src_mp4 = workdir / "source.mp4"
    hook_mp4 = workdir / "hook.mp4"
    bridge_mp4 = workdir / "bridge.mp4"
    ai_mp4 = workdir / "ai.mp4"
    final_mp4 = workdir / "final.mp4"
    tts_mp3 = workdir / "tts.mp3"
    ass_file = workdir / "subs.ass"

    try:
        # 1-2. Download + cut hook
        logger.info(f"strategy D: download {source_url}")
        try:
            downloaded, meta = download_video(source_url, out_dir=workdir)
        except DownloadError as e:
            raise StrategyDError(f"download failed: {e}")
        if downloaded != src_mp4:
            shutil.copy(downloaded, src_mp4)

        full_duration = _probe_duration(src_mp4)
        hook_len = min(hook_seconds, max(2.0, full_duration - 1.0))
        _cut(src_mp4, 0.0, hook_len, hook_mp4)

        # 3. Transcript
        try:
            transcript = transcribe_audio(src_mp4, user.openai_api_key)
        except TranscribeError as e:
            logger.warning(f"transcribe failed, using empty: {e}")
            transcript = ""

        # 4. LLM reply
        reply = _build_reply(
            openai_api_key=user.openai_api_key,
            original_transcript=transcript,
            brand=brand, product_description=product_description,
            extra_instructions=extra_instructions,
        )
        reply_text = reply["reply_text"]
        bridge_text = reply["bridge_caption"]

        # 5. TTS
        ai_duration = _synthesize_tts(
            reply_text, openai_api_key=user.openai_api_key,
            out_path=tts_mp3, voice=tts_voice,
        )

        # 6. 2 gpt-image-1 keyframes
        kf_prompt_parts = [
            "Photorealistic vertical 9:16 portrait of a young person speaking to camera, "
            "modern apartment background, soft natural light, casual styling,",
        ]
        if face_description:
            kf_prompt_parts.append(f"face: {face_description},")
        if brand:
            kf_prompt_parts.append(f"holding product branded {brand},")
        if product_description:
            kf_prompt_parts.append(f"product: {product_description}")
        kf_prompt = " ".join(kf_prompt_parts)

        kf1 = workdir / "kf1.png"
        kf2 = workdir / "kf2.png"
        _gen_keyframe(kf_prompt + " — opening shot, three-quarter angle",
                      user.openai_api_key, kf1)
        _gen_keyframe(kf_prompt + " — closing shot, smiling, product visible",
                      user.openai_api_key, kf2)

        # 7. Build ai segment (slideshow + tts + subtitles)
        ass_file.write_text(_ass_for_text(reply_text, ai_duration), encoding="utf-8")
        _build_ai_segment([kf1, kf2], tts_mp3, ai_duration, ass_file, ai_mp4, workdir)

        # 8. Bridge card (1 sec)
        _bridge_card(bridge_text, 1.0, bridge_mp4)

        # 9. Concat hook + bridge + ai
        _concat_three([hook_mp4, bridge_mp4, ai_mp4], final_mp4)

        # 10. R2 upload
        key = f"users/{user.id}/forge_d/{uuid.uuid4().hex[:12]}.mp4"
        with final_mp4.open("rb") as f:
            r2.upload_bytes(key, f.read(), content_type="video/mp4")
        media_url = r2.get_proxy_url(key)

        cost_usd = round(
            0.04 * 2          # gpt-image-1 ×2
            + 0.005           # LLM gen
            + 0.003           # Whisper short transcript
            + 0.015,          # tts-1 ~150 words
            3,
        )

        gv = GeneratedVideo(
            user_id=user.id,
            provider=VideoProvider.MOCK,
            status=GenerationStatus.READY,
            prompt=f"[strategy=D react hook={hook_len:.1f}s ai={ai_duration:.1f}s] "
                   f"source={source_url}",
            media_url=media_url,
            media_storage_key=key,
            completed_at=datetime.utcnow(),
        )
        db.add(gv)
        db.commit()
        db.refresh(gv)
        logger.info(f"✅ strategy D done: gv #{gv.id} → {media_url}")

        return {
            "gv_id": gv.id,
            "media_url": media_url,
            "source_title": (meta or {}).get("title"),
            "cost_usd": cost_usd,
            "hook_seconds": hook_len,
            "ai_seconds": ai_duration,
            "reply_text": reply_text,
            "bridge_caption": bridge_text,
        }
    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except OSError:
            pass


# ─── Async wrapper (same pattern as strategy C) ─────────

_d_executor = None

def _get_d_executor():
    global _d_executor
    if _d_executor is None:
        from concurrent.futures import ThreadPoolExecutor
        _d_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="forge_d")
    return _d_executor


def _async_run_strategy_d(
    user_id: int,
    *,
    source_url: str,
    brand: Optional[str],
    product_description: Optional[str],
    face_description: Optional[str],
    extra_instructions: Optional[str],
    hook_seconds: float,
    tts_voice: str,
    gv_id: int,
):
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.error(f"async_strategy_d: user #{user_id} disappeared")
            return
        placeholder = db.query(GeneratedVideo).filter(GeneratedVideo.id == gv_id).first()
        if not placeholder:
            logger.error(f"async_strategy_d: gv #{gv_id} disappeared")
            return
        try:
            result = run_strategy_d(
                db, user,
                source_url=source_url, brand=brand,
                product_description=product_description,
                face_description=face_description,
                extra_instructions=extra_instructions,
                hook_seconds=hook_seconds, tts_voice=tts_voice,
            )
            real = db.query(GeneratedVideo).filter(
                GeneratedVideo.id == result["gv_id"]
            ).first()
            if real and real.id != placeholder.id:
                placeholder.media_url = real.media_url
                placeholder.media_storage_key = real.media_storage_key
                placeholder.provider = real.provider
                placeholder.status = real.status
                placeholder.completed_at = real.completed_at
                placeholder.prompt = real.prompt
                db.delete(real)
                db.commit()
                logger.info(f"async_strategy_d: gv #{placeholder.id} READY "
                            f"(squashed real #{real.id})")
            else:
                db.commit()
        except StrategyDError as e:
            placeholder.status = GenerationStatus.FAILED
            placeholder.error_message = str(e)[:1000]
            placeholder.completed_at = datetime.utcnow()
            db.commit()
            logger.warning(f"async_strategy_d: gv #{placeholder.id} FAILED: {e}")
        except Exception as e:
            placeholder.status = GenerationStatus.FAILED
            placeholder.error_message = f"{type(e).__name__}: {str(e)[:900]}"
            placeholder.completed_at = datetime.utcnow()
            db.commit()
            logger.exception(f"async_strategy_d: gv #{placeholder.id} crashed")
    finally:
        db.close()


def start_strategy_d_async(
    db: Session,
    user: User,
    *,
    source_url: str,
    brand: Optional[str] = None,
    product_description: Optional[str] = None,
    face_description: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    hook_seconds: float = 5.0,
    tts_voice: str = "nova",
) -> int:
    if not user.openai_api_key:
        raise StrategyDError("Strategy D нужен openai_api_key в профиле")

    gv = GeneratedVideo(
        user_id=user.id,
        provider=VideoProvider.MOCK,
        status=GenerationStatus.RUNNING,
        prompt=f"[strategy=D react] source={source_url}",
    )
    db.add(gv)
    db.commit()
    db.refresh(gv)
    gv_id = gv.id

    _get_d_executor().submit(
        _async_run_strategy_d,
        user.id, source_url=source_url, brand=brand,
        product_description=product_description,
        face_description=face_description,
        extra_instructions=extra_instructions,
        hook_seconds=hook_seconds, tts_voice=tts_voice, gv_id=gv_id,
    )
    logger.info(f"start_strategy_d_async: gv #{gv_id} queued")
    return gv_id
