"""
Scene detection через ffmpeg select filter.

Возвращает список cut'ов как [{start: float, end: float}, ...].
Никаких ML — чистый порог изменения кадра. Для рилсов 5-60с этого
достаточно, чтобы понять «сколько шотов и какой длины».
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class SceneError(Exception):
    pass


_PTS_RE = re.compile(r"pts_time:(\d+\.?\d*)")


def detect_scenes(
    media_path: str | Path,
    threshold: float = 0.30,
    timeout: int = 120,
) -> list[dict]:
    """Найти сцены через ffmpeg `select='gt(scene,T)'`.

    Возвращает [{start: 0.0, end: 1.23}, {start: 1.23, end: 3.5}, ...].
    Последний сегмент закрывается duration-ом всего видео.

    threshold=0.30 — стандарт для коротких видео; больше → меньше сцен.
    """
    path = Path(media_path)
    if not path.exists():
        raise SceneError(f"media file not found: {path}")

    # Длительность видео — для закрытия последнего сегмента
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if probe.returncode != 0:
        raise SceneError(f"ffprobe failed: {probe.stderr[:200]}")
    try:
        duration = float(probe.stdout.strip())
    except (ValueError, TypeError):
        raise SceneError(f"bad duration: {probe.stdout!r}")

    # ffmpeg печатает scene-detect события на stderr через showinfo filter
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path),
         "-filter:v", f"select='gt(scene,{threshold})',showinfo",
         "-f", "null", "-"],
        capture_output=True, text=True, check=False, timeout=timeout,
    )
    # stderr содержит лог
    boundaries: list[float] = []
    for line in r.stderr.split("\n"):
        m = _PTS_RE.search(line)
        if m:
            try:
                t = float(m.group(1))
                if 0.05 < t < duration - 0.05:  # отсекаем граничные
                    boundaries.append(round(t, 3))
            except ValueError:
                continue
    boundaries = sorted(set(boundaries))
    cuts = [0.0] + boundaries + [round(duration, 3)]
    scenes = [
        {"start": cuts[i], "end": cuts[i + 1],
         "duration": round(cuts[i + 1] - cuts[i], 3)}
        for i in range(len(cuts) - 1)
    ]
    logger.info(f"detected {len(scenes)} scenes in {path.name} "
                f"(total {duration:.1f}s, threshold {threshold})")
    return scenes
