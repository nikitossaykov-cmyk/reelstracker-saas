"""Cutaway insertion stage — replaces a time range of the talking-head
video with a still bottle-hero (or with a clip from a real B-roll).

Ported from /opt/tg-bot-mimic/tools/reel_factory/bin/cutaway.py with
the CLI dropped — same ffmpeg filter_complex strategy (trim base
segments, optional slow-zoom on inserted stills, concat into a single
stream while audio continues through untouched).

This service operates on bytes / writes through a temp dir; the worker
is responsible for fetching the lipsync + bottle-hero artifacts from
R2 and uploading the result back.

The helper's R&D fixed timing on cutaway at 10s-13s of the talking-
head — that's the "3-sec bottle hero" beat in the 24-sec formula
(handoff §44). When/if we want multiple cutaways we extend the
insertion list, but MVP is one insertion at fixed range.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class CutawayError(RuntimeError):
    pass


def _video_duration_seconds(path: Path) -> float:
    r = subprocess.run(
        [
            FFPROBE, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise CutawayError(
            f"ffprobe failed (rc={r.returncode}): {r.stderr.strip()[:200]}"
        )
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise CutawayError(f"ffprobe returned non-numeric: {r.stdout.strip()!r}")


def apply_image_cutaway(
    *,
    base_video: Path,
    insert_image: Path,
    out_path: Path,
    start_seconds: float = 10.0,
    end_seconds: float = 13.0,
    width: int = 720,
    height: int = 1280,
    fps: int = 30,
) -> Path:
    """Replace [start..end] of base video with a still-zoom of insert_image.

    Audio of base_video plays through unchanged across the whole timeline.
    Output is faststart-ready, libx264 yuv420p, AAC 128k.
    """
    if not base_video.exists():
        raise CutawayError(f"base video missing: {base_video}")
    if not insert_image.exists():
        raise CutawayError(f"insert image missing: {insert_image}")

    total = _video_duration_seconds(base_video)
    if end_seconds <= start_seconds:
        raise CutawayError(
            f"end_seconds ({end_seconds}) must be > start ({start_seconds})"
        )
    if end_seconds > total:
        end_seconds = total
        if end_seconds <= start_seconds:
            raise CutawayError(
                f"base video ({total:.2f}s) shorter than cutaway start"
            )
    insert_dur = end_seconds - start_seconds

    # Three segments: base[0..start] + image[insert_dur] + base[end..total]
    cmd: list[str] = [
        FFMPEG, "-y",
        "-i", str(base_video),
        "-loop", "1", "-t", f"{insert_dur:.3f}", "-i", str(insert_image),
    ]

    seg_base_pre = (
        f"[0:v]trim=start=0:end={start_seconds},setpts=PTS-STARTPTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,fps={fps},format=yuv420p[s0]"
    )
    # Static still — Ken-Burns dropped after Railway ffmpeg version
    # rejected the t-dependent crop expression. 3-sec still on a
    # bottle-hero close-up reads fine without the slow zoom.
    seg_image = (
        f"[1:v]trim=duration={insert_dur:.3f},setpts=PTS-STARTPTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,fps={fps},format=yuv420p[s1]"
    )
    seg_base_post = (
        f"[0:v]trim=start={end_seconds}:end={total},setpts=PTS-STARTPTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,fps={fps},format=yuv420p[s2]"
    )
    concat = "[s0][s1][s2]concat=n=3:v=1:a=0[vout]"
    fc = ";".join([seg_base_pre, seg_image, seg_base_post, concat])

    cmd += [
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise CutawayError(
            f"ffmpeg cutaway failed (rc={r.returncode}): {r.stderr[-800:]}"
        )
    return out_path


def apply_video_cutaway(
    *,
    base_video: Path,
    insert_video: Path,
    out_path: Path,
    start_seconds: float = 10.0,
    end_seconds: float = 13.0,
    width: int = 720,
    height: int = 1280,
    fps: int = 30,
) -> Path:
    """Replace [start..end] of base video with frames from insert_video.

    Audio of base_video plays through unchanged across the whole timeline
    (insert_video's audio is dropped). If insert_video is shorter than
    end-start, it loops; if longer, it gets trimmed from the start.
    Output is faststart-ready.
    """
    if not base_video.exists():
        raise CutawayError(f"base video missing: {base_video}")
    if not insert_video.exists():
        raise CutawayError(f"insert video missing: {insert_video}")

    total = _video_duration_seconds(base_video)
    if end_seconds <= start_seconds:
        raise CutawayError(
            f"end_seconds ({end_seconds}) must be > start ({start_seconds})"
        )
    if end_seconds > total:
        end_seconds = total
        if end_seconds <= start_seconds:
            raise CutawayError(
                f"base video ({total:.2f}s) shorter than cutaway start"
            )
    insert_dur = end_seconds - start_seconds

    cmd: list[str] = [
        FFMPEG, "-y",
        "-i", str(base_video),
        # -stream_loop -1 lets the insert loop if shorter than insert_dur
        "-stream_loop", "-1", "-t", f"{insert_dur:.3f}", "-i", str(insert_video),
    ]

    base_pre = (
        f"[0:v]trim=start=0:end={start_seconds},setpts=PTS-STARTPTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,fps={fps},format=yuv420p[s0]"
    )
    insert = (
        f"[1:v]trim=duration={insert_dur:.3f},setpts=PTS-STARTPTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,fps={fps},format=yuv420p[s1]"
    )
    base_post = (
        f"[0:v]trim=start={end_seconds}:end={total},setpts=PTS-STARTPTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,fps={fps},format=yuv420p[s2]"
    )
    concat = "[s0][s1][s2]concat=n=3:v=1:a=0[vout]"
    fc = ";".join([base_pre, insert, base_post, concat])

    cmd += [
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise CutawayError(
            f"ffmpeg video-cutaway failed (rc={r.returncode}): {r.stderr[-800:]}"
        )
    return out_path
