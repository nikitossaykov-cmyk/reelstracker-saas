"""Unit tests for runpod_pod helpers.

No live RunPod. All subprocess / socket calls are mocked. The DB-aware
stop_pod_if_idle path is skipped because it requires the app DB models;
the pure ensure_pod_up / read_pod_info paths are fully covered.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services.runpod_pod import (
    PodInfo,
    PodUnavailable,
    ensure_pod_up,
    read_pod_info,
)


def test_read_pod_info_parses_kv_file(tmp_path):
    f = tmp_path / "info"
    f.write_text("host=1.2.3.4\nssh_port=2222\nstarted_at=1700000000\n")
    info = read_pod_info(str(f))
    assert info.host == "1.2.3.4"
    assert info.ssh_port == 2222
    assert info.started_at == 1700000000


def test_read_pod_info_missing_returns_none(tmp_path):
    assert read_pod_info(str(tmp_path / "nope")) is None


def test_read_pod_info_no_host_returns_none(tmp_path):
    f = tmp_path / "info"
    f.write_text("ssh_port=22\n")
    assert read_pod_info(str(f)) is None


def test_ensure_pod_up_returns_info_if_reachable(tmp_path, monkeypatch):
    f = tmp_path / "info"
    f.write_text("host=1.2.3.4\nssh_port=22\n")
    monkeypatch.setenv("WAN_POD_INFO", str(f))
    with patch("app.services.runpod_pod._ping", return_value=True):
        info = ensure_pod_up()
    assert info.host == "1.2.3.4"


def test_ensure_pod_up_calls_start_script_when_unreachable(
    tmp_path, monkeypatch,
):
    f = tmp_path / "info"
    f.write_text("host=1.2.3.4\nssh_port=22\n")
    monkeypatch.setenv("WAN_POD_INFO", str(f))
    monkeypatch.setenv("WAN_POD_START_SCRIPT", "/usr/bin/true")
    ping_calls = [False, True]
    with patch("subprocess.run",
               return_value=MagicMock(returncode=0, stdout="", stderr="")), \
         patch("app.services.runpod_pod._ping",
               side_effect=lambda *a, **kw: ping_calls.pop(0)):
        info = ensure_pod_up(start_timeout=5)
    assert info.host == "1.2.3.4"


def test_ensure_pod_up_raises_when_start_script_missing(monkeypatch):
    monkeypatch.setenv("WAN_POD_INFO", "/nope/nada/missing")
    monkeypatch.setenv("WAN_POD_START_SCRIPT", "/nope/also/missing")
    with patch("app.services.runpod_pod._ping", return_value=False):
        with pytest.raises(PodUnavailable):
            ensure_pod_up(start_timeout=2)


def test_ensure_pod_up_raises_when_start_script_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("WAN_POD_INFO", str(tmp_path / "missing"))
    monkeypatch.setenv("WAN_POD_START_SCRIPT", "/usr/bin/false")
    with patch("subprocess.run",
               return_value=MagicMock(returncode=1, stdout="", stderr="boom")), \
         patch("app.services.runpod_pod._ping", return_value=False):
        with pytest.raises(PodUnavailable):
            ensure_pod_up(start_timeout=2)
