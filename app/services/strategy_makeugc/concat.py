"""Final concat — hook (B&W viral donor) + talking-head with cutaway.

Ported from /opt/tg-bot-mimic/tools/reel_factory/bin/concat.py. Re-
encodes each part to a common 720×1280 / 30fps / yuv420p target before
concatenating so codec/resolution drift between the donor hook and the
generated lipsync mp4 doesn't break the timeline.

Output is faststart-ready for direct IG/TT upload.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


FFMPEG = "ffmpeg"


class ConcatError(RuntimeError):
    pass


def concat_parts(
    *,
    parts: list[Path],
    out_path: Path,
    width: int = 720,
    height: int = 1280,
    fps: int = 30,
) -> Path:
    """Concatenate `parts` into out_path. Each part is normalised first."""
    if len(parts) < 2:
        raise ConcatError(f"need at least 2 parts, got {len(parts)}")
    for p in parts:
        if not p.exists():
            raise ConcatError(f"part missing: {p}")

    cmd: list[str] = [FFMPEG, "-y"]
    for p in parts:
        cmd += ["-i", str(p)]

    filters: list[str] = []
    cats_v: list[str] = []
    cats_a: list[str] = []
    for i in range(len(parts)):
        filters.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
            f"setsar=1,fps={fps},format=yuv420p[v{i}]"
        )
        filters.append(
            f"[{i}:a]aresample=44100,aformat=channel_layouts=stereo[a{i}]"
        )
        cats_v.append(f"[v{i}]")
        cats_a.append(f"[a{i}]")
    filters.append(
        "".join(cats_v) + f"concat=n={len(parts)}:v=1:a=0[vout]"
    )
    filters.append(
        "".join(cats_a) + f"concat=n={len(parts)}:v=0:a=1[aout]"
    )
    fc = ";".join(filters)

    cmd += [
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ConcatError(
            f"ffmpeg concat failed (rc={r.returncode}): {r.stderr[-800:]}"
        )
    return out_path
