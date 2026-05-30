"""
ComfyUI client + SSH-bridge для MSI.

MSI = домашний ноут Ника в Tailscale (100.118.157.108), на нём крутится
ComfyUI с ReActor / Wav2Lip / XTTS / LivePortrait / LatentSync. Бесплатный
GPU-стек для post-processing Forge-генераций (face swap, lip sync, voice
clone) без затрат на Runway/Veo.

Архитектура:
- HTTP API на http://100.118.157.108:8188 (POST /prompt, GET /history/{id})
- SCP для загрузки input файлов в ComfyUI/input и забора результатов из
  ComfyUI/output

Хост настраивается через env MSI_HOST (default 100.118.157.108) и
MSI_SSH_USER (default Maxim). Если MSI недоступен — функции бросают
MSINotReachable и worker может зафейлить gracefully.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


MSI_HOST = os.environ.get("MSI_HOST", "100.118.157.108")
MSI_SSH_USER = os.environ.get("MSI_SSH_USER", "Maxim")
MSI_COMFY_URL = f"http://{MSI_HOST}:8188"
MSI_INPUT_DIR = "C:/Users/Maxim/ComfyUI/input"
MSI_OUTPUT_DIR = "C:/Users/Maxim/ComfyUI/output"
SCP_TIMEOUT = 120
POLL_TIMEOUT = 600
POLL_INTERVAL = 5


class MSINotReachable(Exception):
    pass


class ComfyWorkflowError(Exception):
    pass


def _ssh(cmd: str, capture: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    full = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", f"ConnectTimeout=10",
            f"{MSI_SSH_USER}@{MSI_HOST}", cmd]
    return subprocess.run(full, capture_output=capture, text=True, timeout=timeout, check=False)


def _scp_to_msi(local: Path, remote_relpath: str) -> None:
    """SCP файла в ComfyUI/input. remote_relpath — относительно user home."""
    full = ["scp", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            str(local), f"{MSI_SSH_USER}@{MSI_HOST}:{remote_relpath}"]
    r = subprocess.run(full, capture_output=True, text=True, timeout=SCP_TIMEOUT, check=False)
    if r.returncode != 0:
        raise MSINotReachable(f"scp to MSI failed: {r.stderr[:300]}")


def _scp_from_msi(remote_path: str, local: Path) -> None:
    full = ["scp", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{MSI_SSH_USER}@{MSI_HOST}:{remote_path}", str(local)]
    r = subprocess.run(full, capture_output=True, text=True, timeout=SCP_TIMEOUT, check=False)
    if r.returncode != 0:
        raise MSINotReachable(f"scp from MSI failed: {r.stderr[:300]}")


def ping_msi() -> bool:
    """Быстрый health-check: GET /system_stats."""
    import requests
    try:
        r = requests.get(f"{MSI_COMFY_URL}/system_stats", timeout=8)
        return r.status_code == 200
    except Exception:
        return False


def upload_to_msi_input(local_path: Path, remote_filename: Optional[str] = None) -> str:
    """Загрузить файл в ComfyUI/input/. Возвращает имя файла (для inputs в workflow)."""
    if not local_path.exists():
        raise ValueError(f"local file not found: {local_path}")
    fname = remote_filename or f"reelstracker_{uuid.uuid4().hex[:8]}_{local_path.name}"
    # SCP в C:\Users\Maxim\ComfyUI\input через POSIX-style path (OpenSSH on Windows понимает)
    _scp_to_msi(local_path, f"ComfyUI/input/{fname}")
    return fname


def submit_workflow(prompt: dict) -> str:
    """POST workflow → /prompt. Возвращает prompt_id."""
    import requests
    body = {"prompt": prompt, "client_id": f"reelstracker-{uuid.uuid4().hex[:8]}"}
    r = requests.post(f"{MSI_COMFY_URL}/prompt", json=body, timeout=30)
    if r.status_code != 200:
        raise ComfyWorkflowError(f"submit failed {r.status_code}: {r.text[:300]}")
    data = r.json()
    pid = data.get("prompt_id")
    if not pid:
        raise ComfyWorkflowError(f"no prompt_id in response: {data}")
    if data.get("node_errors"):
        raise ComfyWorkflowError(f"node_errors: {json.dumps(data['node_errors'])[:500]}")
    return pid


def wait_for_completion(prompt_id: str, timeout: int = POLL_TIMEOUT) -> dict:
    """Поллит /history/{prompt_id} пока в нём не появится наш prompt."""
    import requests
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        r = requests.get(f"{MSI_COMFY_URL}/history/{prompt_id}", timeout=15)
        if r.status_code != 200:
            continue
        data = r.json()
        if prompt_id in data:
            entry = data[prompt_id]
            status = entry.get("status", {})
            if status.get("status_str") == "success":
                return entry
            if status.get("status_str") == "error":
                msgs = status.get("messages", [])
                err = next((m[1] for m in msgs if isinstance(m, list) and m[0] == "execution_error"), {})
                raise ComfyWorkflowError(f"workflow error: {err.get('exception_message', msgs)}")
    raise ComfyWorkflowError(f"workflow timeout after {timeout}s")


def find_output_video(history_entry: dict) -> Optional[str]:
    """В outputs ищем gifs/videos с filename — последний (он же финальный)."""
    outputs = history_entry.get("outputs", {})
    for node_id, out in outputs.items():
        for key in ("gifs", "videos", "images"):
            items = out.get(key, [])
            for item in items:
                fn = item.get("filename")
                if fn and fn.endswith((".mp4", ".webm", ".mkv")):
                    return fn
    return None


def download_msi_output(filename: str, local_path: Path) -> None:
    """Качаем результат из ComfyUI/output/{filename}."""
    _scp_from_msi(f"ComfyUI/output/{filename}", local_path)


def cleanup_msi_input(filenames: list[str]) -> None:
    """Удалить uploaded inputs после успешного выполнения."""
    if not filenames:
        return
    paths = " ".join(f'"ComfyUI\\input\\{f}"' for f in filenames)
    _ssh(f"cmd /c del {paths}", timeout=15)


def load_workflow_template(name: str) -> dict:
    """Загрузить JSON workflow template из package."""
    here = Path(__file__).parent / "comfyui_workflows"
    path = here / f"{name}.json"
    if not path.exists():
        raise ValueError(f"workflow template not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))
