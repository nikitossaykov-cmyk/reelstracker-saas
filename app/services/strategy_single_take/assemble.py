"""ffmpeg assembly for the single-take reel.

Recipe = vault build_v36.py without gesture inserts:
  normalize (720×1280 crop-fill, 30fps) → optional hook concat →
  burn ASS captions → polish (body ×1.05 + film grain, hook untouched,
  NO handheld shake — Nick's UGC-style rule: рилсы снимают со штатива).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# crop-fill (not pad): matches the vault recipe — talking head fills frame
VF_NORMALIZE = (
    "scale=720:1280:force_original_aspect_ratio=increase,"
    "crop=720:1280,fps=30"
)


class AssembleError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssembleError(
            f"ffmpeg failed (rc={r.returncode}): {r.stderr[-800:]}"
        )


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise AssembleError(f"ffprobe failed: {r.stderr[-300:]}")
    return float(r.stdout.strip())


def normalize_clip(src: Path, dst: Path) -> Path:
    _run([FFMPEG, "-y", "-v", "error", "-i", str(src),
          "-vf", VF_NORMALIZE,
          "-c:v", "libx264", "-preset", "fast", "-crf", "20",
          "-c:a", "aac", "-ar", "44100", "-ac", "2", str(dst)])
    return dst


def concat_clips(parts: list[Path], dst: Path) -> Path:
    """Concat demuxer over already-normalized parts (same codec/fps)."""
    lst = dst.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    _run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
          "-i", str(lst),
          "-c:v", "libx264", "-preset", "fast", "-crf", "20",
          "-c:a", "aac", "-ar", "44100", "-ac", "2", str(dst)])
    return dst


def burn_captions(src: Path, ass_path: Path, dst: Path) -> Path:
    _run([FFMPEG, "-y", "-v", "error", "-i", str(src),
          "-vf", f"ass={ass_path}",
          "-c:v", "libx264", "-preset", "fast", "-crf", "20",
          "-c:a", "copy", str(dst)])
    return dst


def build_polish_filter(*, hook_seconds: float) -> str:
    """Body ×1.05 + grain; hook (if any) passes through untouched."""
    if hook_seconds <= 0:
        return (
            "[0:v]setpts=(PTS-STARTPTS)/1.05,noise=alls=5:allf=t[v];"
            "[0:a]asetpts=PTS-STARTPTS,atempo=1.05[a]"
        )
    h = hook_seconds
    return (
        f"[0:v]trim=0:{h},setpts=PTS-STARTPTS[hv];"
        f"[0:a]atrim=0:{h},asetpts=PTS-STARTPTS[ha];"
        f"[0:v]trim={h},setpts=(PTS-STARTPTS)/1.05,noise=alls=5:allf=t[bv];"
        f"[0:a]atrim={h},asetpts=PTS-STARTPTS,atempo=1.05[ba];"
        f"[hv][ha][bv][ba]concat=n=2:v=1:a=1[v][a]"
    )


def polish(src: Path, dst: Path, *, hook_seconds: float) -> Path:
    _run([FFMPEG, "-y", "-v", "error", "-i", str(src),
          "-filter_complex", build_polish_filter(hook_seconds=hook_seconds),
          "-map", "[v]", "-map", "[a]",
          "-c:v", "libx264", "-preset", "fast", "-crf", "20",
          "-c:a", "aac", "-ar", "44100", "-ac", "2",
          "-movflags", "+faststart", str(dst)])
    return dst


def detect_silences_cmd(audio: Path, *, noise: str, min_d: float) -> list[str]:
    """The silencedetect invocation; caller captures stderr and feeds it
    to captions.parse_silencedetect."""
    return [FFMPEG, "-hide_banner", "-i", str(audio),
            "-af", f"silencedetect=noise={noise}:d={min_d}",
            "-f", "null", "-"]


def detect_silences(audio: Path, *, noise: str, min_d: float) -> str:
    r = subprocess.run(
        detect_silences_cmd(audio, noise=noise, min_d=min_d),
        capture_output=True, text=True,
    )
    return r.stderr
