"""
Vision-описание видео через GPT-4o-mini.

OpenAI пока не принимает видео напрямую — извлекаем N равномерно
расположенных кадров через ffmpeg, отправляем как image-grid в один
chat.completions.create call. На 5-30 секундный reel хватает 4-6 кадров
для описания «что в кадре, какой стиль, что происходит».

Стоимость: gpt-4o-mini ~$0.15/M input tokens. 6 кадров @720p ~ 4K
tokens → ~$0.0006 за рилс. Дёшево.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class VisionError(Exception):
    """Любая ошибка vision-аналайзера, нормализованная."""


def _extract_frames(media_path: Path, count: int = 6) -> list[Path]:
    """N равномерно распределённых кадров через ffmpeg → temp jpg."""
    if not media_path.exists():
        raise VisionError(f"media file not found: {media_path}")

    # Получаем длительность через ffprobe (предполагаем что ffmpeg доступен)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(media_path)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if probe.returncode != 0:
        raise VisionError(f"ffprobe failed: {probe.stderr[:200]}")
    try:
        duration = float(probe.stdout.strip())
    except (ValueError, TypeError):
        raise VisionError(f"ffprobe bad duration: {probe.stdout!r}")

    tmpdir = Path(tempfile.mkdtemp(prefix="vis_"))
    frames: list[Path] = []
    for i in range(count):
        # Равномерно: [duration/count/2, ...] — середины N равных интервалов
        t = duration * (i + 0.5) / count
        out = tmpdir / f"frame_{i:02d}.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-ss", str(t), "-i", str(media_path),
             "-frames:v", "1", "-vf", "scale=720:-2",
             "-q:v", "3", str(out)],
            capture_output=True, text=True, check=False, timeout=60,
        )
        if r.returncode == 0 and out.exists():
            frames.append(out)
    if not frames:
        raise VisionError("ffmpeg failed to extract any frames")
    return frames


def _encode_data_url(path: Path) -> str:
    data = path.read_bytes()
    return f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"


SYSTEM_PROMPT = (
    "You are analysing TikTok/Instagram Reels frames. Produce a tight, "
    "factual description of what's shown across the frames: "
    "characters (gender/age/style), setting/location, what's happening "
    "(hook → middle → ending), camera technique, lighting, colour palette, "
    "any on-screen text or product, and the overall mood. "
    "Be specific (≤200 words). Russian or English — pick whichever fits "
    "the on-screen text."
)


def summarize_video(
    media_path: str | Path,
    openai_api_key: str,
    frame_count: int = 6,
    model: str = "gpt-4o-mini",
    timeout: int = 120,
) -> str:
    """Извлечь N кадров → описать видео одним абзацем."""
    if not openai_api_key:
        raise VisionError("openai_api_key пуст — нечем авторизоваться")

    frames = _extract_frames(Path(media_path), count=frame_count)
    try:
        from openai import OpenAI
    except ImportError as e:
        raise VisionError(f"openai SDK не установлен: {e}")
    client = OpenAI(api_key=openai_api_key, timeout=timeout)

    content: list[dict] = [
        {"type": "text",
         "text": f"Describe this short video based on {len(frames)} key frames."},
    ]
    for f in frames:
        content.append({"type": "image_url",
                        "image_url": {"url": _encode_data_url(f), "detail": "low"}})

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            max_tokens=400,
            temperature=0.3,
        )
    except Exception as e:
        raise VisionError(f"OpenAI {type(e).__name__}: {str(e)[:300]}")
    finally:
        # cleanup temp frames
        for f in frames:
            try:
                f.unlink()
            except OSError:
                pass
        try:
            frames[0].parent.rmdir()
        except OSError:
            pass

    text = (resp.choices[0].message.content or "").strip()
    logger.info(f"vision summary {Path(media_path).name}: {len(text)} chars")
    return text
