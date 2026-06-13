"""Strategy E, Mode 2 — wraps the existing wan_clone.py RunPod workflow.

The owner's `wan_clone.py` CLI handles the heavy lifting:
  - upload donor.mp4 + face.png to the RunPod ComfyUI pod
  - submit the Wan-2.2 Animate workflow
  - poll for the result
  - download to out.mp4

This wrapper:
  - ensures the pod is up (autostart if needed via runpod_pod)
  - invokes the CLI with the right args
  - normalises failures into a single WanCloneError for the worker
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.services.runpod_pod import ensure_pod_up, PodUnavailable


WAN_CLONE_SCRIPT_ENV = "WAN_CLONE_SCRIPT"
DEFAULT_SCRIPT = "/opt/tg-bot/tools/wan_clone.py"


class WanCloneError(RuntimeError):
    pass


def run_mode2(
    *,
    donor: Path,
    face: Path,
    out: Path,
    timeout: int = 900,
) -> Path:
    try:
        ensure_pod_up(start_timeout=120)
    except PodUnavailable as e:
        raise WanCloneError(f"RunPod pod unavailable: {e}") from e

    script = os.getenv(WAN_CLONE_SCRIPT_ENV, DEFAULT_SCRIPT)
    cmd = [script, str(donor), str(face), "--out", str(out)]
    r = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout
    )
    if r.returncode != 0:
        raise WanCloneError(
            f"wan_clone exit={r.returncode}: {r.stderr.strip()[:300]}"
        )
    if not out.exists() or out.stat().st_size == 0:
        raise WanCloneError("wan_clone produced no output file")
    return out
