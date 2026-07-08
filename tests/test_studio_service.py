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
        cutaways_enabled=False,   # tests opt in explicitly via **over
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
    def fake_assemble(job, tmp, lipsync_path, voiceover_path, hook_path, insert_paths=None):
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

    def fake_assemble(job, tmp, lipsync_path, voiceover_path, hook_path, insert_paths=None):
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


def _patch_pre_cutaway_stages(monkeypatch, w):
    monkeypatch.setattr(w, "generate_studio_portrait", lambda **kw: (b"p", 0.15))
    monkeypatch.setattr(w, "generate_voiceover_v3", lambda **kw: b"a")
    monkeypatch.setattr(w, "generate_lipsync", lambda **kw: (b"v", 0.74))

    def fake_assemble(job, tmp, lipsync_path, voiceover_path, hook_path, insert_paths=None):
        out = tmp / "final.mp4"
        out.write_bytes(b"f")
        return out
    monkeypatch.setattr(w, "_assemble", fake_assemble)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "e-key")
    monkeypatch.setenv("MAKEUGC_DEFAULT_VOICE_ID", "v-id")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def test_worker_cutaways_happy_path(db_session, test_user, fake_r2_worker, monkeypatch):
    import app.workers.studio_worker as w
    _patch_pre_cutaway_stages(monkeypatch, w)
    monkeypatch.setattr(
        w, "generate_cutaway_still", lambda **kw: (b"still-" + kw["kind"].encode(), 0.15),
    )
    monkeypatch.setattr(
        w, "animate_cutaway", lambda **kw: (b"clip-" + kw["kind"].encode(), 0.25),
    )
    j = _make_pending_job(db_session, test_user, fake_r2_worker, cutaways_enabled=True)
    w.process_job(db_session, j, test_user)

    assert j.status == StudioStatus.READY
    assert j.cap_still_key and j.spray_still_key
    assert j.cap_clip_key and j.spray_clip_key
    assert fake_r2_worker.blobs[j.cap_clip_key] == b"clip-cap_off"
    assert fake_r2_worker.blobs[j.spray_clip_key] == b"clip-spray"
    # 0.15 portrait + 0.74 lipsync + tts + 2×(0.15+0.25)
    assert float(j.cost_usd) == pytest.approx(0.15 + 0.74 + 0.0072 + 0.80, abs=0.01)


def test_worker_cutaways_disabled_skips_stage(db_session, test_user, fake_r2_worker, monkeypatch):
    import app.workers.studio_worker as w
    _patch_pre_cutaway_stages(monkeypatch, w)

    def boom(**kw):
        raise AssertionError("cutaways must not run")
    monkeypatch.setattr(w, "generate_cutaway_still", boom)
    j = _make_pending_job(db_session, test_user, fake_r2_worker, cutaways_enabled=False)
    w.process_job(db_session, j, test_user)
    assert j.status == StudioStatus.READY
    assert j.cap_still_key is None and j.cap_clip_key is None


def test_worker_cutaway_failure_is_non_blocking(db_session, test_user, fake_r2_worker, monkeypatch):
    import app.workers.studio_worker as w
    _patch_pre_cutaway_stages(monkeypatch, w)

    def boom(**kw):
        raise RuntimeError("kling упал")
    monkeypatch.setattr(w, "generate_cutaway_still", boom)
    monkeypatch.setattr(w, "animate_cutaway", boom)
    j = _make_pending_job(db_session, test_user, fake_r2_worker, cutaways_enabled=True)
    w.process_job(db_session, j, test_user)
    assert j.status == StudioStatus.READY      # reel ships without inserts
    assert j.cap_clip_key is None and j.spray_clip_key is None


def test_worker_cutaway_animation_failure_keeps_still(db_session, test_user, fake_r2_worker, monkeypatch):
    import app.workers.studio_worker as w
    _patch_pre_cutaway_stages(monkeypatch, w)
    monkeypatch.setattr(w, "generate_cutaway_still", lambda **kw: (b"still", 0.15))

    def boom(**kw):
        raise RuntimeError("kling упал")
    monkeypatch.setattr(w, "animate_cutaway", boom)
    j = _make_pending_job(db_session, test_user, fake_r2_worker, cutaways_enabled=True)
    w.process_job(db_session, j, test_user)
    assert j.status == StudioStatus.READY
    assert j.cap_still_key and j.spray_still_key   # stills survive for static fallback
    assert j.cap_clip_key is None


def test_assemble_splices_inserts_and_shifts_captions(monkeypatch, tmp_path):
    """_assemble with insert clips: body split at the VO gap, inserts
    concatenated between halves, captions after the split shifted right."""
    import app.workers.studio_worker as w

    calls = {"cut": [], "concat": None, "ass": None}

    monkeypatch.setattr(w, "normalize_clip", lambda src, dst: (dst.write_bytes(b"n"), dst)[1])
    monkeypatch.setattr(w, "probe_duration", lambda p: 10.0)
    # VO: speech 0-5 and 7.5-10 → gap 5.0-7.5, midpoint 6.25
    monkeypatch.setattr(w, "detect_silences", lambda p, noise, min_d: (
        "[x] silence_start: 5.0\n[x] silence_end: 7.5 | silence_duration: 2.5\n"
    ))

    def fake_cut(src, dst, *, start, end=None):
        calls["cut"].append((start, end))
        dst.write_bytes(b"c")
        return dst
    monkeypatch.setattr(w, "cut_clip", fake_cut)

    def fake_concat(parts, dst):
        calls["concat"] = [p.name for p in parts]
        dst.write_bytes(b"cc")
        return dst
    monkeypatch.setattr(w, "concat_clips", fake_concat)

    def fake_burn(src, ass_path, dst):
        calls["ass"] = ass_path.read_text()
        dst.write_bytes(b"b")
        return dst
    monkeypatch.setattr(w, "burn_captions", fake_burn)
    monkeypatch.setattr(w, "polish", lambda src, dst, *, hook_seconds: (dst.write_bytes(b"p"), dst)[1])

    j = StudioJob(
        user_id=1, product_image_keys=["k"], product_name="X", brand="Y",
        price_rub=Decimal("1"), dupe_price_rub=Decimal("2"),
        script_text="Раз. Два", voice_style="normal",
        captions_enabled=True, cutaways_enabled=True,
        status=StudioStatus.ASSEMBLE, cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    tmp = tmp_path
    lipsync = tmp / "lipsync.mp4"; lipsync.write_bytes(b"l")
    vo = tmp / "vo.mp3"; vo.write_bytes(b"v")
    cap_ins = tmp / "cap_ins.mp4"; cap_ins.write_bytes(b"i1")
    spray_ins = tmp / "spray_ins.mp4"; spray_ins.write_bytes(b"i2")

    out = w._assemble(j, tmp, lipsync, vo, None, insert_paths=[cap_ins, spray_ins])
    assert out.read_bytes() == b"p"
    # body split at gap midpoint 6.25: (0, 6.25) then (6.25, None)
    assert (0.0, 6.25) in calls["cut"] and (6.25, None) in calls["cut"]
    # inserts trimmed to 1.2s: two cuts (0, 1.2)
    assert calls["cut"].count((0.0, 1.2)) == 2
    # concat order: body_a, insert1, insert2, body_b (no hook)
    assert calls["concat"] == ["body_a.mp4", "ins_0.mp4", "ins_1.mp4", "body_b.mp4"]
    # caption «Два» (span 7.5-10) shifted right by 2×1.2s → starts ≥ 9.9
    assert "0:00:09.90" in calls["ass"]


def test_assemble_no_gap_falls_back_to_straight_body(monkeypatch, tmp_path):
    import app.workers.studio_worker as w
    monkeypatch.setattr(w, "normalize_clip", lambda src, dst: (dst.write_bytes(b"n"), dst)[1])
    monkeypatch.setattr(w, "probe_duration", lambda p: 10.0)
    # continuous speech → no gap → pick_insert_gap None
    monkeypatch.setattr(w, "detect_silences", lambda p, noise, min_d: "")

    def no_cut(*a, **kw):
        raise AssertionError("must not split without a gap")
    monkeypatch.setattr(w, "cut_clip", no_cut)
    monkeypatch.setattr(w, "burn_captions", lambda src, ass, dst: (dst.write_bytes(b"b"), dst)[1])
    monkeypatch.setattr(w, "polish", lambda src, dst, *, hook_seconds: (dst.write_bytes(b"p"), dst)[1])

    j = StudioJob(
        user_id=1, product_image_keys=["k"], product_name="X", brand="Y",
        price_rub=Decimal("1"), dupe_price_rub=Decimal("2"),
        script_text="Раз", voice_style="normal",
        captions_enabled=True, cutaways_enabled=True,
        status=StudioStatus.ASSEMBLE, cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    lipsync = tmp_path / "l.mp4"; lipsync.write_bytes(b"l")
    vo = tmp_path / "v.mp3"; vo.write_bytes(b"v")
    ins = tmp_path / "i.mp4"; ins.write_bytes(b"i")
    out = w._assemble(j, tmp_path, lipsync, vo, None, insert_paths=[ins])
    assert out.read_bytes() == b"p"


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


def _patch_post_portrait_stages(monkeypatch, w):
    monkeypatch.setattr(w, "generate_voiceover_v3", lambda **kw: b"a")
    monkeypatch.setattr(w, "generate_lipsync", lambda **kw: (b"v", 0.74))

    def fake_assemble(job, tmp, lipsync_path, voiceover_path, hook_path, insert_paths=None):
        out = tmp / "final.mp4"
        out.write_bytes(b"f")
        return out
    monkeypatch.setattr(w, "_assemble", fake_assemble)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "e-key")
    monkeypatch.setenv("MAKEUGC_DEFAULT_VOICE_ID", "v-id")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def test_worker_persona_reuse(db_session, test_user, fake_r2_worker, monkeypatch):
    import app.workers.studio_worker as w
    _patch_post_portrait_stages(monkeypatch, w)

    test_user.studio_persona_key = "users/1/studio/4/portrait-abc.jpg"
    db_session.commit()
    fake_r2_worker.blobs["users/1/studio/4/portrait-abc.jpg"] = b"persona-jpg"

    seen = {}
    def fake_persona(**kw):
        seen.update(kw)
        return b"portrait-with-persona", 0.15
    monkeypatch.setattr(w, "generate_persona_portrait", fake_persona)
    def boom(**kw):
        raise AssertionError("не должен звать generate_studio_portrait")
    monkeypatch.setattr(w, "generate_studio_portrait", boom)

    j = _make_pending_job(db_session, test_user, fake_r2_worker, use_persona=True)
    w.process_job(db_session, j, test_user)

    assert j.status == StudioStatus.READY
    assert seen["persona_bytes"] == b"persona-jpg"
    assert seen["look_prompt"] is None
    assert fake_r2_worker.blobs[j.portrait_key] == b"portrait-with-persona"
    # канон без смены образа не трогаем (анти-дрифт)
    db_session.refresh(test_user)
    assert test_user.studio_persona_key == "users/1/studio/4/portrait-abc.jpg"


def test_worker_persona_look_updates_canon(db_session, test_user, fake_r2_worker, monkeypatch):
    import app.workers.studio_worker as w
    _patch_post_portrait_stages(monkeypatch, w)

    test_user.studio_persona_key = "users/1/studio/4/portrait-abc.jpg"
    db_session.commit()
    fake_r2_worker.blobs["users/1/studio/4/portrait-abc.jpg"] = b"persona-jpg"

    seen = {}
    def fake_persona(**kw):
        seen.update(kw)
        return b"new-look", 0.15
    monkeypatch.setattr(w, "generate_persona_portrait", fake_persona)

    j = _make_pending_job(
        db_session, test_user, fake_r2_worker,
        use_persona=True, look_prompt="белый топ",
    )
    w.process_job(db_session, j, test_user)

    assert j.status == StudioStatus.READY
    assert seen["look_prompt"] == "белый топ"
    db_session.refresh(test_user)
    assert test_user.studio_persona_key == j.portrait_key  # новый образ = новый канон


def test_worker_persona_missing_falls_back(db_session, test_user, fake_r2_worker, monkeypatch):
    import app.workers.studio_worker as w
    _patch_post_portrait_stages(monkeypatch, w)

    assert test_user.studio_persona_key is None
    monkeypatch.setattr(w, "generate_studio_portrait", lambda **kw: (b"fresh", 0.15))
    def boom(**kw):
        raise AssertionError("нет персоны — нечего референсить")
    monkeypatch.setattr(w, "generate_persona_portrait", boom)

    j = _make_pending_job(db_session, test_user, fake_r2_worker, use_persona=True)
    w.process_job(db_session, j, test_user)

    assert j.status == StudioStatus.READY
    db_session.refresh(test_user)
    assert test_user.studio_persona_key is None  # канон только явным сохранением


def test_api_persona_get_and_save(auth_client, db_session, test_user):
    r = auth_client.get("/api/studio/persona")
    assert r.status_code == 200
    assert r.json()["persona_key"] is None

    j = StudioJob(
        user_id=test_user.id,
        product_image_keys=["k"],
        product_name="X", brand="Y",
        price_rub=Decimal("1"), dupe_price_rub=Decimal("2"),
        voice_style="normal", captions_enabled=True,
        portrait_key="users/1/studio/4/portrait-abc.jpg",
        status=StudioStatus.READY,
        cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    db_session.add(j)
    db_session.commit()

    r = auth_client.post("/api/studio/persona", json={"job_id": j.id})
    assert r.status_code == 200
    assert r.json()["persona_key"] == "users/1/studio/4/portrait-abc.jpg"
    db_session.refresh(test_user)
    assert test_user.studio_persona_key == "users/1/studio/4/portrait-abc.jpg"

    r = auth_client.get("/api/studio/persona")
    assert r.json()["persona_key"] == "users/1/studio/4/portrait-abc.jpg"


def test_api_persona_save_rejects_bad_jobs(auth_client, db_session, test_user):
    # чужого/несуществующего job'а нет → 404
    r = auth_client.post("/api/studio/persona", json={"job_id": 99999})
    assert r.status_code == 404
    # свой, но без портрета → 400
    j = StudioJob(
        user_id=test_user.id,
        product_image_keys=["k"],
        product_name="X", brand="Y",
        price_rub=Decimal("1"), dupe_price_rub=Decimal("2"),
        voice_style="normal", captions_enabled=True,
        status=StudioStatus.PENDING,
        cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    db_session.add(j)
    db_session.commit()
    r = auth_client.post("/api/studio/persona", json={"job_id": j.id})
    assert r.status_code == 400


def test_api_create_with_persona_flags(auth_client, db_session, monkeypatch):
    import app.services.studio_service as svc
    monkeypatch.setattr(svc, "get_r2", lambda: FakeR2())
    r = auth_client.post(
        "/api/studio/jobs/",
        files=[("product_images", ("p.jpg", JPEG, "image/jpeg"))],
        data={
            "product_name": "X", "brand": "Y",
            "price_rub": "1990", "dupe_price_rub": "16000",
            "use_persona": "true",
            "look_prompt": "белый топ",
        },
    )
    assert r.status_code == 202, r.text
    j = db_session.get(StudioJob, r.json()["id"])
    assert j.use_persona is True
    assert j.look_prompt == "белый топ"

    r2 = auth_client.post(
        "/api/studio/jobs/",
        files=[("product_images", ("p.jpg", JPEG, "image/jpeg"))],
        data={
            "product_name": "X", "brand": "Y",
            "price_rub": "1990", "dupe_price_rub": "16000",
            "use_persona": "false",
        },
    )
    assert r2.status_code == 202, r2.text
    j2 = db_session.get(StudioJob, r2.json()["id"])
    assert j2.use_persona is False
    assert j2.look_prompt is None


def test_studio_persona_columns(db_session, test_user):
    assert test_user.studio_persona_key is None
    test_user.studio_persona_key = "users/1/studio/4/portrait-abc.jpg"
    j = StudioJob(
        user_id=test_user.id,
        product_image_keys=["k"],
        product_name="X", brand="Y",
        price_rub=Decimal("1"), dupe_price_rub=Decimal("2"),
        voice_style="normal", captions_enabled=True,
        look_prompt="белый топ",
        status=StudioStatus.PENDING,
        cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    db_session.add(j)
    db_session.commit()
    db_session.refresh(j)
    db_session.refresh(test_user)
    assert j.use_persona is True               # python default
    assert j.look_prompt == "белый топ"
    assert test_user.studio_persona_key == "users/1/studio/4/portrait-abc.jpg"


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
