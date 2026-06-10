"""
MP4 +faststart utility.

Browsers (especially Safari/iOS) need the `moov` atom near the start
of the file to discover duration, codecs, fps. ffmpeg defaults put it
at the END, which breaks playback when delivered over our streaming
proxy (Range requests can't pre-fetch the tail before the head).

`ensure_faststart()` is idempotent: if moov is already before mdat,
it's a no-op. Otherwise it runs `ffmpeg -c copy -movflags +faststart`
which is fast (no re-encode, just remux).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def is_faststart(path: Path) -> bool:
    """Return True if moov atom appears before mdat — i.e. file is
    already laid out for progressive streaming."""
    try:
        data = path.open("rb").read(16 * 1024 * 1024)  # 16MB enough for either
    except OSError:
        return False
    pos = 0
    seen_moov = False
    seen_mdat = False
    while pos + 8 <= len(data):
        sz = int.from_bytes(data[pos:pos + 4], "big")
        name = data[pos + 4:pos + 8]
        if name == b"moov":
            seen_moov = True
            if not seen_mdat:
                return True
        elif name == b"mdat":
            seen_mdat = True
            if not seen_moov:
                return False
        if sz <= 0:
            break
        pos += sz
    return False


def ensure_faststart(src: Path, *, timeout: int = 120) -> bool:
    """Idempotently rewrite `src` so moov precedes mdat. Returns True
    if file was rewritten, False if it was already OK or rewrite failed
    (errors are logged, original file left untouched)."""
    if is_faststart(src):
        return False
    tmp = src.with_suffix(src.suffix + ".faststart.tmp")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src),
        "-c", "copy", "-movflags", "+faststart",
        str(tmp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        logger.warning(
            f"ensure_faststart failed for {src.name} "
            f"(rc={r.returncode}): {r.stderr[:200]}"
        )
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    tmp.replace(src)
    logger.info(f"ensure_faststart: rewrote {src.name} ({src.stat().st_size} bytes)")
    return True
