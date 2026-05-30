"""
Uniqualizer — пресет ffmpeg-фильтров, ломающий перцептивные хэши
(pHash, audio fingerprint) и тривиальную сэммплинг-детекцию платформ.

Порт логики из /opt/tg-bot/tools/wan_clone.py apply_uniq().

Цель — снизить вероятность что IG/TikTok/VK пометят наш ремейк
как «дубликат» (visual или audio). НЕ ломает воспроизведение и
сохраняет читаемость on-screen текста (без hflip / heavy crop).

Применяется опционально перед публикацией (или сразу при сохранении
remake-результата, если задан флаг auto_uniqify в Post).
"""

from __future__ import annotations

import logging
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class UniqifyPreset:
    """Параметры визуальной/аудио маскировки."""
    hue_shift: int = 10                  # ±degrees
    saturation: float = 1.08             # x mult
    brightness: float = 0.025            # +/- (eq filter)
    contrast: float = 1.05               # x mult
    scale_factor: float = 1.04           # >1.0 zoom in slightly before crop
    rotate_deg: float = 1.0              # small CW rotation, borders cropped
    speed_factor: float = 1.05           # >1.0 = faster playback
    noise_strength: int = 4              # ffmpeg noise filter alls=
    audio_pitch_semitones: float = 0.5   # tiny pitch shift (±semitones)
    crf: int = 22                        # h264 quality 18-28
    preset: str = "fast"                 # x264 preset
    strip_metadata: bool = True
    fake_creation_time: str = "2026-05-20T14:32:11"

    def randomise(self, seed: Optional[int] = None) -> "UniqifyPreset":
        """Лёгкая рандомизация — чтобы два соседних ролика не имели
        идентичных параметров (тоже как сигнатура)."""
        rng = random.Random(seed)
        return UniqifyPreset(
            hue_shift=self.hue_shift + rng.randint(-3, 3),
            saturation=round(self.saturation + rng.uniform(-0.03, 0.03), 3),
            brightness=round(self.brightness + rng.uniform(-0.01, 0.01), 4),
            contrast=round(self.contrast + rng.uniform(-0.02, 0.02), 3),
            scale_factor=round(self.scale_factor + rng.uniform(-0.005, 0.005), 4),
            rotate_deg=round(self.rotate_deg + rng.uniform(-0.3, 0.3), 3),
            speed_factor=round(self.speed_factor + rng.uniform(-0.01, 0.01), 4),
            noise_strength=max(1, self.noise_strength + rng.randint(-1, 2)),
            audio_pitch_semitones=round(self.audio_pitch_semitones + rng.uniform(-0.15, 0.15), 3),
            crf=self.crf + rng.randint(-1, 1),
            preset=self.preset,
            strip_metadata=self.strip_metadata,
            fake_creation_time=self.fake_creation_time,
        )

    def video_filter(self) -> str:
        """Собрать -vf chain из параметров."""
        import math
        rad = self.rotate_deg * math.pi / 180
        crop_after_rotate = max(2, int(8 + abs(self.rotate_deg) * 4))
        parts = [
            f"hue=h={self.hue_shift}:s={self.saturation}",
            f"eq=brightness={self.brightness}:contrast={self.contrast}",
            f"scale=iw*{self.scale_factor}:ih*{self.scale_factor}",
            f"crop=in_w/{self.scale_factor}:in_h/{self.scale_factor}",
            f"rotate={rad}:fillcolor=black:ow=rotw({rad}):oh=roth({rad})",
            f"crop=iw-{crop_after_rotate}:ih-{crop_after_rotate}",
            f"setpts=PTS/{self.speed_factor}",
            f"noise=alls={self.noise_strength}:allf=t",
        ]
        return ",".join(parts)

    def audio_filter(self) -> str:
        """asetrate-based pitch shift + atempo для компенсации длительности."""
        # 1 semitone ≈ rate ratio ~1.05946
        ratio = 2.0 ** (self.audio_pitch_semitones / 12.0)
        # tempo compensation чтобы видео и аудио оставались синхронны после
        # setpts/speed_factor: итоговая скорость аудио = ratio * (1/speed_compensation)
        # → atempo = self.speed_factor / ratio
        atempo = self.speed_factor / ratio
        # atempo limited to [0.5, 2.0] per filter; для нашего диапазона ок
        return f"asetrate=44100*{ratio:.4f},aresample=44100,atempo={atempo:.4f}"


class UniqifyError(Exception):
    pass


def uniqify_video(
    src_path: str | Path,
    out_path: str | Path,
    preset: Optional[UniqifyPreset] = None,
    randomise: bool = True,
    seed: Optional[int] = None,
    timeout: int = 600,
) -> Path:
    """Применить uniq-преcет к src_path → записать в out_path."""
    if shutil.which("ffmpeg") is None:
        raise UniqifyError("ffmpeg not in PATH — install or fix Dockerfile")
    src = Path(src_path)
    if not src.exists():
        raise UniqifyError(f"source not found: {src}")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    eff = preset or UniqifyPreset()
    if randomise:
        eff = eff.randomise(seed=seed)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src),
        "-vf", eff.video_filter(),
        "-af", eff.audio_filter(),
    ]
    if eff.strip_metadata:
        cmd += ["-map_metadata", "-1",
                "-metadata", f"creation_time={eff.fake_creation_time}"]
    cmd += [
        "-c:v", "libx264", "-crf", str(eff.crf), "-preset", eff.preset,
        "-c:a", "aac", "-b:a", "128k",
        str(out),
    ]
    logger.info(f"uniqify {src.name} → {out.name} (hue={eff.hue_shift}, "
                f"speed={eff.speed_factor}, rot={eff.rotate_deg}°)")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if r.returncode != 0:
        raise UniqifyError(f"ffmpeg failed (rc={r.returncode}): {r.stderr[:400]}")
    if not out.exists() or out.stat().st_size == 0:
        raise UniqifyError("ffmpeg returned 0 but output is empty/missing")
    return out
