"""Studio POC — service + worker orchestration tests."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

# Module-level import so the table registers on Base before conftest's
# create_all runs (same reason the other DB test files import models here).
from app.models.studio_job import StudioJob, StudioStatus


def test_studio_job_roundtrip(db_session, test_user):
    j = StudioJob(
        user_id=test_user.id,
        product_image_keys=["users/1/studio/x/product-1.jpg"],
        product_name="WHITE CHOCOLATE",
        brand="dose",
        price_rub=Decimal("1990"),
        dupe_price_rub=Decimal("16000"),
        voice_style="asmr",
        captions_enabled=True,
        status=StudioStatus.PENDING,
        cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    db_session.add(j)
    db_session.commit()
    db_session.refresh(j)

    assert j.id is not None
    assert j.status == StudioStatus.PENDING
    assert j.judge_score is None
    assert j.hook_video_key is None


class FakeR2:
    """Minimal stand-in for app.core.storage.R2 used by service+worker."""
    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.bucket = "test"
        self._client = self

    def upload_bytes(self, key, blob, content_type=None):
        self.blobs[key] = blob

    def get_object(self, Bucket=None, Key=None):
        import io
        return {"Body": io.BytesIO(self.blobs[Key])}


@pytest.fixture
def fake_r2(monkeypatch):
    r2 = FakeR2()
    import app.services.studio_service as svc
    monkeypatch.setattr(svc, "get_r2", lambda: r2)
    return r2


JPEG = b"\xff\xd8\xff fake-jpeg-bytes"


def test_create_studio_job(db_session, test_user, fake_r2):
    from app.services.studio_service import create_studio_job_async

    j = create_studio_job_async(
        db_session, test_user,
        product_images=[(JPEG, "image/jpeg")],
        product_name="WHITE CHOCOLATE",
        brand="Richard Maison",
        price_rub=Decimal("1990"),
        dupe_price_rub=Decimal("16000"),
        script_text=None,
        voice_style="asmr",
        captions_enabled=True,
        hook_video=(b"\x00fakemp4", "video/mp4"),
    )
    assert j.status == StudioStatus.PENDING
    assert len(j.product_image_keys) == 1
    assert j.product_image_keys[0] in fake_r2.blobs
    assert j.hook_video_key in fake_r2.blobs
    assert j.voice_style == "asmr"


def test_create_studio_job_validation(db_session, test_user, fake_r2):
    from app.services.studio_service import (
        StudioValidationError, create_studio_job_async,
    )
    with pytest.raises(StudioValidationError):
        create_studio_job_async(
            db_session, test_user,
            product_images=[],
            product_name="X", brand="Y",
            price_rub=Decimal("1990"), dupe_price_rub=Decimal("16000"),
            script_text=None, voice_style="normal",
            captions_enabled=True, hook_video=None,
        )
    with pytest.raises(StudioValidationError):
        create_studio_job_async(
            db_session, test_user,
            product_images=[(JPEG, "image/jpeg")],
            product_name="X", brand="Y",
            price_rub=Decimal("1990"), dupe_price_rub=Decimal("16000"),
            script_text=None, voice_style="opera",  # invalid
            captions_enabled=True, hook_video=None,
        )


def _make_pending_job(db_session, test_user, fake_r2_worker, **over):
    fake_r2_worker.blobs["k/product-1.jpg"] = JPEG
    fields = dict(
        user_id=test_user.id,
        product_image_keys=["k/product-1.jpg"],
        product_name="WHITE CHOCOLATE",
        brand="Richard Maison",
        price_rub=Decimal("1990"),
        dupe_price_rub=Decimal("16000"),
        script_text="Я это заказала. Ну что?",
        voice_style="asmr",
        captions_enabled=False,   # skip captions → no silencedetect in unit test
        status=StudioStatus.PENDING,
        cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    fields.update(over)
    j = StudioJob(**fields)
    db_session.add(j)
    db_session.commit()
    db_session.refresh(j)
    return j


@pytest.fixture
def fake_r2_worker(monkeypatch):
    r2 = FakeR2()
    import app.workers.studio_worker as w
    monkeypatch.setattr(w, "get_r2", lambda: r2)
    return r2


def test_worker_happy_path(db_session, test_user, fake_r2_worker, monkeypatch, tmp_path):
    import app.workers.studio_worker as w

    monkeypatch.setattr(
        w, "generate_studio_portrait",
        lambda **kw: (b"portrait-bytes", 0.15),
    )
    monkeypatch.setattr(
        w, "generate_voiceover_v3", lambda **kw: b"mp3-bytes",
    )
    monkeypatch.setattr(
        w, "generate_lipsync", lambda **kw: (b"lipsync-mp4", 0.74),
    )
    # assemble: pretend ffmpeg produced a final file
    def fake_assemble(job, tmp, lipsync_path, voiceover_path, hook_path):
        out = tmp / "final.mp4"
        out.write_bytes(b"final-mp4")
        return out
    monkeypatch.setattr(w, "_assemble", fake_assemble)
    monkeypatch.setattr(
        w, "judge_video",
        lambda path, api_key, brief=None: {"overall": 8, "verdict": "pass"},
    )
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "e-key")
    monkeypatch.setenv("MAKEUGC_DEFAULT_VOICE_ID", "v-id")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r-key")

    j = _make_pending_job(db_session, test_user, fake_r2_worker)
    w.process_job(db_session, j, test_user)

    assert j.status == StudioStatus.READY
    assert j.portrait_key and j.voiceover_key and j.lipsync_key and j.output_key
    assert j.judge_score == 8
    assert j.judge_report["verdict"] == "pass"
    assert float(j.cost_usd) == pytest.approx(0.15 + 0.74 + 0.0072, abs=0.01)
    assert fake_r2_worker.blobs[j.output_key] == b"final-mp4"


def test_worker_judge_failure_is_non_blocking(
    db_session, test_user, fake_r2_worker, monkeypatch, tmp_path,
):
    import app.workers.studio_worker as w
    from app.services.strategy_single_take.judge import JudgeError

    monkeypatch.setattr(w, "generate_studio_portrait", lambda **kw: (b"p", 0.15))
    monkeypatch.setattr(w, "generate_voiceover_v3", lambda **kw: b"a")
    monkeypatch.setattr(w, "generate_lipsync", lambda **kw: (b"v", 0.74))

    def fake_assemble(job, tmp, lipsync_path, voiceover_path, hook_path):
        out = tmp / "final.mp4"
        out.write_bytes(b"f")
        return out
    monkeypatch.setattr(w, "_assemble", fake_assemble)

    def boom(path, api_key, brief=None):
        raise JudgeError("квота")
    monkeypatch.setattr(w, "judge_video", boom)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "e-key")
    monkeypatch.setenv("MAKEUGC_DEFAULT_VOICE_ID", "v-id")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r-key")

    j = _make_pending_job(db_session, test_user, fake_r2_worker)
    w.process_job(db_session, j, test_user)

    assert j.status == StudioStatus.READY   # judge failure ≠ job failure
    assert j.judge_score is None


def test_worker_stage_failure_marks_failed(
    db_session, test_user, fake_r2_worker, monkeypatch,
):
    import app.workers.studio_worker as w

    def boom(**kw):
        raise RuntimeError("nano-banana упал")
    monkeypatch.setattr(w, "generate_studio_portrait", boom)
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r-key")

    j = _make_pending_job(db_session, test_user, fake_r2_worker)
    w.process_job(db_session, j, test_user)

    assert j.status == StudioStatus.FAILED
    assert "portrait" in j.error_message
