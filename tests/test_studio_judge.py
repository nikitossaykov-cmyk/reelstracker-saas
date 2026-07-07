"""Judge fallback rotation — mocked HTTP, no network."""
from __future__ import annotations

import json

import pytest

from app.services.strategy_single_take import judge as judge_mod


class FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise judge_mod.requests.HTTPError(f"HTTP {self.status_code}")


def _gemini_ok(report: dict) -> FakeResp:
    return FakeResp(200, {
        "candidates": [{"content": {"parts": [{"text": json.dumps(report)}]}}]
    })


REPORT = {"overall": 8, "verdict": "pass", "scores": {"hook": 7},
          "top_issues": [], "timeline_notes": []}


def test_judge_first_model_succeeds(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        return _gemini_ok(REPORT)

    monkeypatch.setattr(judge_mod.requests, "post", fake_post)
    res = judge_mod.judge_uploaded("files/abc", key="k", brief=None)
    assert res["overall"] == 8
    assert len(calls) == 1
    assert judge_mod.JUDGE_MODELS[0] in calls[0]


def test_judge_rotates_on_429_then_succeeds(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        # exact segment: "gemini-2.5-flash" is a substring of "-flash-lite"
        if f"models/{judge_mod.JUDGE_MODELS[0]}:" in url:
            return FakeResp(429)
        return _gemini_ok(REPORT)

    monkeypatch.setattr(judge_mod.requests, "post", fake_post)
    res = judge_mod.judge_uploaded("files/abc", key="k", brief=None)
    assert res["verdict"] == "pass"
    assert len(calls) == 2
    assert judge_mod.JUDGE_MODELS[1] in calls[1]


def test_judge_all_models_exhausted_raises(monkeypatch):
    monkeypatch.setattr(
        judge_mod.requests, "post", lambda url, **kw: FakeResp(503),
    )
    with pytest.raises(judge_mod.JudgeError):
        judge_mod.judge_uploaded("files/abc", key="k", brief=None)


def test_judge_non_quota_error_raises_immediately(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        return FakeResp(400)

    monkeypatch.setattr(judge_mod.requests, "post", fake_post)
    with pytest.raises(judge_mod.JudgeError):
        judge_mod.judge_uploaded("files/abc", key="k", brief=None)
    assert len(calls) == 1  # no pointless rotation on hard 400
