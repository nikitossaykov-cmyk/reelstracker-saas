"""RunPod pod lifecycle helpers used by Strategy E Mode 2.

The pod_info file and the start/stop scripts live OUTSIDE this repo
(currently at /opt/tg-bot/tools/ on the owner's VPS). Paths are
overridable via env so this code works in any environment that has
the scripts available.

ensure_pod_up is idempotent: returns immediately if the pod is already
reachable; otherwise invokes the start script and polls until the pod
becomes reachable or the timeout expires.

stop_pod_if_idle is intended to be called from a periodic tick. It
consults the DB for the most recent Strategy E Mode 2 activity; if no
RUNNING row has been touched within the idle window, the pod is
stopped to avoid RunPod billing for nothing.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


WAN_POD_INFO_ENV = "WAN_POD_INFO"
WAN_POD_START_ENV = "WAN_POD_START_SCRIPT"
WAN_POD_STOP_ENV = "WAN_POD_STOP_SCRIPT"

DEFAULT_INFO_PATH = "/root/.wan_pod_info"
DEFAULT_START_PATH = "/opt/tg-bot/tools/pod_start.sh"
DEFAULT_STOP_PATH = "/opt/tg-bot/tools/pod_stop.sh"


class PodUnavailable(Exception):
    pass


@dataclass
class PodInfo:
    host: str
    ssh_port: int
    started_at: Optional[int] = None


def read_pod_info(path: str) -> Optional[PodInfo]:
    p = Path(path)
    if not p.exists():
        return None
    kv: dict[str, str] = {}
    for line in p.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    if "host" not in kv:
        return None
    return PodInfo(
        host=kv["host"],
        ssh_port=int(kv.get("ssh_port", "22")),
        started_at=int(kv["started_at"]) if "started_at" in kv else None,
    )


def _ping(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_pod_up(start_timeout: int = 120) -> PodInfo:
    info_path = os.getenv(WAN_POD_INFO_ENV, DEFAULT_INFO_PATH)
    info = read_pod_info(info_path)
    if info and _ping(info.host, info.ssh_port):
        return info

    start_script = os.getenv(WAN_POD_START_ENV, DEFAULT_START_PATH)
    if not Path(start_script).exists():
        raise PodUnavailable(f"start script not found: {start_script}")

    r = subprocess.run(
        [start_script], capture_output=True, text=True, timeout=180
    )
    if r.returncode != 0:
        raise PodUnavailable(
            f"start script failed: {r.stderr.strip()[:200]}"
        )

    deadline = time.time() + start_timeout
    while time.time() < deadline:
        info = read_pod_info(info_path)
        if info and _ping(info.host, info.ssh_port):
            return info
        time.sleep(3)
    raise PodUnavailable("pod did not become reachable within timeout")


def stop_pod_if_idle(max_idle_minutes: int = 10) -> bool:
    info_path = os.getenv(WAN_POD_INFO_ENV, DEFAULT_INFO_PATH)
    info = read_pod_info(info_path)
    if not info or not _ping(info.host, info.ssh_port):
        return False

    from app.database import SessionLocal
    from app.models.generation import GeneratedVideo

    threshold = datetime.utcnow() - timedelta(minutes=max_idle_minutes)
    db = SessionLocal()
    try:
        recent = (
            db.query(GeneratedVideo)
            .filter(
                GeneratedVideo.mode == 2,
                GeneratedVideo.started_at > threshold,
            )
            .first()
        )
        if recent:
            return False
    finally:
        db.close()

    stop_script = os.getenv(WAN_POD_STOP_ENV, DEFAULT_STOP_PATH)
    if not Path(stop_script).exists():
        return False
    r = subprocess.run(
        [stop_script], capture_output=True, text=True, timeout=60
    )
    return r.returncode == 0
