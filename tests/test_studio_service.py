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


def test_api_create_and_list_and_retry(auth_client, db_session, test_user, fake_r2):
    r = auth_client.post(
        "/api/studio/jobs/",
        files={"product_images": ("p.jpg", JPEG, "image/jpeg")},
        data={
            "product_name": "WHITE CHOCOLATE",
            "brand": "Richard Maison",
            "price_rub": "1990",
            "dupe_price_rub": "16000",
            "voice_style": "asmr",
            "captions_enabled": "true",
        },
    )
    assert r.status_code == 202, r.text
    jid = r.json()["id"]
    assert r.json()["status"] == "pending"

    r = auth_client.get("/api/studio/jobs/")
    assert r.status_code == 200
    assert any(item["id"] == jid for item in r.json()["items"])

    # simulate a failed job → retry resets to PENDING and clears stage keys
    j = db_session.query(StudioJob).get(jid)
    j.status = StudioStatus.FAILED
    j.error_message = "boom"
    j.portrait_key = "k/p.jpg"
    j.judge_score = 3
    db_session.commit()

    r = auth_client.post(f"/api/studio/jobs/{jid}/retry")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending"
    assert body["portrait_key"] is None
    assert body["judge_score"] is None
    assert body["error_message"] is None


def test_api_cross_user_404(auth_client, db_session, other_user, fake_r2):
    j = StudioJob(
        user_id=other_user.id,
        product_image_keys=["x"],
        product_name="X", brand="Y",
        price_rub=Decimal("1"), dupe_price_rub=Decimal("2"),
        voice_style="normal", captions_enabled=True,
        status=StudioStatus.PENDING, cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    db_session.add(j)
    db_session.commit()
    assert auth_client.get(f"/api/studio/jobs/{j.id}").status_code == 404
    assert auth_client.post(f"/api/studio/jobs/{j.id}/retry").status_code == 404


def test_media_allowlist_covers_studio_keys(db_session, test_user):
    from app.api.media import _verify_key_in_db

    j = StudioJob(
        user_id=test_user.id,
        product_image_keys=["u/7/studio/1/product-1.jpg"],
        product_name="X", brand="Y",
        price_rub=Decimal("1"), dupe_price_rub=Decimal("2"),
        voice_style="normal", captions_enabled=True,
        status=StudioStatus.READY, cost_usd=Decimal("0"),
        output_key="u/7/studio/1/final-abc.mp4",
        portrait_key="u/7/studio/1/portrait-abc.jpg",
        created_at=datetime.utcnow(),
    )
    db_session.add(j)
    db_session.commit()

    assert _verify_key_in_db("u/7/studio/1/final-abc.mp4", db_session)
    assert _verify_key_in_db("u/7/studio/1/portrait-abc.jpg", db_session)
    assert _verify_key_in_db("u/7/studio/1/product-1.jpg", db_session)
    assert not _verify_key_in_db("u/7/studio/1/nonexistent.mp4", db_session)


def test_api_create_with_cutaways_flag(auth_client, db_session, fake_r2):
    r = auth_client.post(
        "/api/studio/jobs/",
        files={"product_images": ("p.jpg", JPEG, "image/jpeg")},
        data={
            "product_name": "X", "brand": "Y",
            "price_rub": "1990", "dupe_price_rub": "16000",
            "voice_style": "normal", "captions_enabled": "true",
            "cutaways_enabled": "false",
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["cutaways_enabled"] is False
    assert body["cap_clip_key"] is None
    j = db_session.query(StudioJob).get(body["id"])
    assert j.cutaways_enabled is False


def test_api_retry_clears_cutaway_keys(auth_client, db_session, test_user, fake_r2):
    j = StudioJob(
        user_id=test_user.id, product_image_keys=["x"],
        product_name="X", brand="Y",
        price_rub=Decimal("1"), dupe_price_rub=Decimal("2"),
        voice_style="normal", captions_enabled=True,
        status=StudioStatus.FAILED, cost_usd=Decimal("0"),
        cap_still_key="k/cs.jpg", spray_still_key="k/ss.jpg",
        cap_clip_key="k/cc.mp4", spray_clip_key="k/sc.mp4",
        created_at=datetime.utcnow(),
    )
    db_session.add(j)
    db_session.commit()
    r = auth_client.post(f"/api/studio/jobs/{j.id}/retry")
    assert r.status_code == 200
    body = r.json()
    for f in ("cap_still_key", "spray_still_key", "cap_clip_key", "spray_clip_key"):
        assert body[f] is None


def test_media_allowlist_covers_cutaway_keys(db_session, test_user):
    from app.api.media import _verify_key_in_db
    j = StudioJob(
        user_id=test_user.id, product_image_keys=["x"],
        product_name="X", brand="Y",
        price_rub=Decimal("1"), dupe_price_rub=Decimal("2"),
        voice_style="normal", captions_enabled=True,
        status=StudioStatus.READY, cost_usd=Decimal("0"),
        cap_still_key="u/7/studio/2/cutaway-cap_off-a.jpg",
        spray_still_key="u/7/studio/2/cutaway-spray-a.jpg",
        cap_clip_key="u/7/studio/2/cutaway-cap_off-a.mp4",
        spray_clip_key="u/7/studio/2/cutaway-spray-a.mp4",
        created_at=datetime.utcnow(),
    )
    db_session.add(j)
    db_session.commit()
    for k in ("u/7/studio/2/cutaway-cap_off-a.jpg",
              "u/7/studio/2/cutaway-spray-a.jpg",
              "u/7/studio/2/cutaway-cap_off-a.mp4",
              "u/7/studio/2/cutaway-spray-a.mp4"):
        assert _verify_key_in_db(k, db_session)


def test_api_script_autogen_passes_cutaways(auth_client, monkeypatch):
    import app.api.studio as api_mod
    seen = {}

    def fake_gen(**kw):
        seen.update(kw)
        return "ок"
    monkeypatch.setattr(api_mod, "generate_studio_script", fake_gen)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    r = auth_client.post("/api/studio/script", json={
        "product_name": "X", "brand": "Y",
        "price_rub": 1990, "dupe_price_rub": 16000,
        "voice_style": "asmr", "cutaways_enabled": False,
    })
    assert r.status_code == 200
    assert seen["cutaways"] is False


def test_studio_job_cutaway_columns(db_session, test_user):
    j = StudioJob(
        user_id=test_user.id,
        product_image_keys=["k"],
        product_name="X", brand="Y",
        price_rub=Decimal("1"), dupe_price_rub=Decimal("2"),
        voice_style="normal", captions_enabled=True,
        status=StudioStatus.CUTAWAYS,
        cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    db_session.add(j)
    db_session.commit()
    db_session.refresh(j)
    assert j.cutaways_enabled is True          # server/python default
    assert j.cap_still_key is None
    assert j.spray_still_key is None
    assert j.cap_clip_key is None
    assert j.spray_clip_key is None
    assert StudioStatus.CUTAWAYS == "cutaways"


def test_api_script_autogen(auth_client, monkeypatch):
    import app.api.studio as api_mod
    monkeypatch.setattr(
        api_mod, "generate_studio_script",
        lambda **kw: "Я это заказала. Ну что?",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    r = auth_client.post("/api/studio/script", json={
        "product_name": "WHITE CHOCOLATE",
        "brand": "Richard Maison",
        "price_rub": 1990,
        "dupe_price_rub": 16000,
        "voice_style": "asmr",
    })
    assert r.status_code == 200
    assert r.json()["script_text"].startswith("Я это заказала")
