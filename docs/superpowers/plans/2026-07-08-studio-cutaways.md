# Studio Action Cutaways Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Studio jobs optionally get two AI-generated action cutaways (cap-off + spray) spliced into the talking-head take at the voiceover pause after the script's promise phrase.

**Architecture:** New optional, non-blocking CUTAWAYS worker stage between LIPSYNC and ASSEMBLE. Stills via nano-banana-pro (portrait + product photo refs), animation via Kling v2.1 standard. Assemble splits the body at the longest speech gap, inserts trimmed clips, shifts captions right.

**Tech Stack:** FastAPI, SQLAlchemy, ffmpeg, Replicate (google/nano-banana-pro, kwaivgi/kling-v2.1-standard), pytest.

**Spec:** `docs/specs/2026-07-08-studio-cutaways-design.md`

---

### Task 1: Model columns + status + migrations

**Files:**
- Modify: `app/models/studio_job.py`
- Modify: `app/main.py` (`run_lightweight_migrations`, list ends ~line 360)
- Test: `tests/test_studio_service.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_studio_service.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /opt/projects/reelstracker-saas && python -m pytest tests/test_studio_service.py::test_studio_job_cutaway_columns -x -q`
Expected: FAIL — `AttributeError: CUTAWAYS`

- [ ] **Step 3: Implement.** In `app/models/studio_job.py`:

In `StudioStatus`, between `LIPSYNC` and `ASSEMBLE`:

```python
    LIPSYNC = "lipsync"
    CUTAWAYS = "cutaways"
    ASSEMBLE = "assemble"
```

In `StudioJob`, after `hook_video_key`:

```python
    cutaways_enabled = Column(Boolean, nullable=False, default=True)
```

After `lipsync_key`:

```python
    cap_still_key = Column(Text, nullable=True)
    spray_still_key = Column(Text, nullable=True)
    cap_clip_key = Column(Text, nullable=True)
    spray_clip_key = Column(Text, nullable=True)
```

In `app/main.py`, append to the `migrations` list (before the closing `]`, after the `makeugc_jobs ... product_image_key DROP NOT NULL` entry):

```python
        # Studio cutaways — action inserts (cap-off + spray)
        "ALTER TABLE studio_jobs ADD COLUMN IF NOT EXISTS cutaways_enabled BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE studio_jobs ADD COLUMN IF NOT EXISTS cap_still_key TEXT",
        "ALTER TABLE studio_jobs ADD COLUMN IF NOT EXISTS spray_still_key TEXT",
        "ALTER TABLE studio_jobs ADD COLUMN IF NOT EXISTS cap_clip_key TEXT",
        "ALTER TABLE studio_jobs ADD COLUMN IF NOT EXISTS spray_clip_key TEXT",
```

(status is `String(24)`, the new enum value needs no DB change.)

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_studio_service.py -x -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/models/studio_job.py app/main.py tests/test_studio_service.py
git commit -m "feat(studio): cutaway columns + CUTAWAYS status + migrations"
```

---

### Task 2: cutaways.py — prompts

**Files:**
- Create: `app/services/strategy_single_take/cutaways.py`
- Test: `tests/test_studio_captions.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_studio_captions.py`:

```python
def test_cutaway_still_prompts():
    from app.services.strategy_single_take.cutaways import build_cutaway_still_prompt
    cap = build_cutaway_still_prompt(
        kind="cap_off", product_name="WHITE CHOCOLATE", brand="dose",
    )
    assert "SAME woman" in cap
    assert "second reference image" in cap
    assert "WHITE CHOCOLATE" in cap and "dose" in cap
    assert "lifting" in cap.lower() and "cap" in cap.lower()
    spray = build_cutaway_still_prompt(
        kind="spray", product_name="WHITE CHOCOLATE", brand="dose",
    )
    assert "mist" in spray.lower()
    assert "pump" in spray.lower()
    with pytest.raises(ValueError):
        build_cutaway_still_prompt(kind="sniff", product_name="X", brand="Y")


def test_cutaway_motion_prompts():
    from app.services.strategy_single_take.cutaways import MOTION_PROMPTS, NEGATIVE_PROMPT
    assert set(MOTION_PROMPTS) == {"cap_off", "spray"}
    assert "mist" in MOTION_PROMPTS["spray"].lower()
    assert "drinking" in NEGATIVE_PROMPT
    assert "kissing" in NEGATIVE_PROMPT
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_studio_captions.py::test_cutaway_still_prompts tests/test_studio_captions.py::test_cutaway_motion_prompts -x -q`
Expected: FAIL — `ModuleNotFoundError` / `ImportError`

- [ ] **Step 3: Create `app/services/strategy_single_take/cutaways.py`:**

```python
"""Action cutaway inserts — cap-off + spray b-roll for the Studio reel.

Recipe proven in vault wc_spritz_clip.py / v36: nano-banana-pro still
with TWO image refs (portrait first, real product photo second) keeps
girl and bottle consistent; Kling i2v animates spray fine but renders
sniff as drinking/kissing — so only cap_off + spray, никакого sniff.
Kling STANDARD: ~11× cheaper than PRO, b-roll quality достаточная.
"""
from __future__ import annotations

import base64

from app.services.replicate_client import ReplicateClient
from app.services.strategy_makeugc.portrait import _extract_output


MODEL_STILL = "google/nano-banana-pro"
MODEL_ANIMATE = "kwaivgi/kling-v2.1"
COST_STILL_USD = 0.15
COST_CLIP_USD = 0.25  # Kling standard 5s — уточнить по факту первого прогона

KINDS = ("cap_off", "spray")


class CutawayError(Exception):
    pass


_ACTIONS = {
    "cap_off": (
        "Both her hands are raised to chest height in front of her: one "
        "hand holds the bottle, the other is lifting the matte black cap "
        "straight up off the bottle — the cap has just separated from the "
        "neck. Her eyes look down at the bottle with curiosity."
    ),
    "spray": (
        "She holds the bottle in her right hand raised toward her neck and "
        "wrist, index finger placed on top of the pump, a fine delicate "
        "mist visible against the light. Her eyes are softly closed, head "
        "tilted slightly back."
    ),
}

MOTION_PROMPTS = {
    "cap_off": (
        "One subtle continuous motion only: her hand slowly lifts the "
        "matte black cap up and away from the bottle, she watches the "
        "bottle. The bottle stays in her other hand throughout, no "
        "morphing, no warping, the label remains readable. Soft ambient "
        "indoor calm, smooth cinematic micro-motion."
    ),
    "spray": (
        "One subtle continuous motion only: her index finger presses down "
        "on the perfume pump once, a fine delicate mist sprays from the "
        "nozzle onto her wrist and neck, the mist catches the light, her "
        "eyes stay softly closed. The bottle stays in her hand throughout, "
        "no morphing, no warping, the label remains readable."
    ),
}

NEGATIVE_PROMPT = (
    "drinking the bottle, kissing the bottle, putting bottle in mouth, "
    "mouth-to-bottle, bottle to lips, eating, distorted hand, deformed "
    "fingers, extra fingers, morphing bottle, melting label, jittery, "
    "low quality, watermark"
)


def build_cutaway_still_prompt(*, kind: str, product_name: str, brand: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown cutaway kind: {kind} (allowed: {KINDS})")
    return (
        "Photorealistic vertical UGC photo: the SAME woman as in the "
        "first reference image — same face, same hair, same clothes, same "
        "room and lighting. She interacts with the EXACT perfume bottle "
        "from the second reference image — keep its shape, cap, color and "
        f"label identical; the label text must read exactly «{brand}» and "
        f"«{product_name}», do not misspell, redraw or invent any text. "
        f"{_ACTIONS[kind]} "
        "Shot on a phone, shallow depth of field, natural skin tones, "
        "vertical 9:16 composition, candid UGC aesthetic."
    )


def _data_uri(blob: bytes, content_type: str = "image/jpeg") -> str:
    return f"data:{content_type};base64,{base64.b64encode(blob).decode()}"


def generate_cutaway_still(
    *,
    portrait_bytes: bytes,
    product_bytes: bytes,
    kind: str,
    product_name: str,
    brand: str,
    replicate_api_key: str,
) -> tuple[bytes | str, float]:
    """nano-banana-pro still: portrait ref first, product ref second."""
    params = {
        "prompt": build_cutaway_still_prompt(
            kind=kind, product_name=product_name, brand=brand,
        ),
        "image_input": [_data_uri(portrait_bytes), _data_uri(product_bytes)],
        "aspect_ratio": "9:16",
        "output_format": "jpg",
    }
    client = ReplicateClient(api_key=replicate_api_key)
    out = client.run_model(MODEL_STILL, params)
    return _extract_output(out), COST_STILL_USD


def animate_cutaway(
    *,
    still_bytes: bytes,
    kind: str,
    replicate_api_key: str,
) -> tuple[bytes | str, float]:
    """Kling v2.1 standard, 5s, still as start frame."""
    if kind not in KINDS:
        raise CutawayError(f"unknown cutaway kind: {kind}")
    params = {
        "mode": "standard",
        "duration": 5,
        "prompt": MOTION_PROMPTS[kind],
        "negative_prompt": NEGATIVE_PROMPT,
        "start_image": _data_uri(still_bytes),
    }
    client = ReplicateClient(api_key=replicate_api_key)
    out = client.run_model(MODEL_ANIMATE, params)
    return _extract_output(out), COST_CLIP_USD
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_studio_captions.py -x -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/strategy_single_take/cutaways.py tests/test_studio_captions.py
git commit -m "feat(studio): cutaways module — nano-banana stills + kling animation"
```

---

### Task 3: captions.pick_insert_gap + shift_captions

**Files:**
- Modify: `app/services/strategy_single_take/captions.py` (append at end)
- Test: `tests/test_studio_captions.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
def test_pick_insert_gap_dominant_gap():
    from app.services.strategy_single_take.captions import pick_insert_gap
    # gaps: 5.0-7.5 (2.5s, midpoint 6.25 = 62.5% of 10) and 8.5-9.0 (0.5s)
    spans = [(0.0, 5.0), (7.5, 8.5), (9.0, 10.0)]
    assert pick_insert_gap(spans, total=10.0) == pytest.approx(6.25)


def test_pick_insert_gap_ignores_gap_outside_window():
    from app.services.strategy_single_take.captions import pick_insert_gap
    # only gap 0.5-1.5: midpoint 1.0 = 10% of 10 → before 20% window
    assert pick_insert_gap([(0.0, 0.5), (1.5, 10.0)], total=10.0) is None
    # only gap 9.0-9.8: midpoint 9.4 = 94% → after 85% window
    assert pick_insert_gap([(0.0, 9.0), (9.8, 10.0)], total=10.0) is None


def test_pick_insert_gap_too_short_or_none():
    from app.services.strategy_single_take.captions import pick_insert_gap
    # longest in-window gap is 0.4s < 0.5s min
    assert pick_insert_gap([(0.0, 5.0), (5.4, 10.0)], total=10.0) is None
    # single span → no gaps at all
    assert pick_insert_gap([(0.0, 10.0)], total=10.0) is None
    assert pick_insert_gap([], total=10.0) is None


def test_shift_captions():
    from app.services.strategy_single_take.captions import shift_captions
    aligned = [(0.0, 2.0, "до"), (3.0, 5.0, "после"), (6.0, 8.0, "хвост")]
    out = shift_captions(aligned, split_at=2.5, inserts_seconds=2.4)
    assert out == [
        (0.0, 2.0, "до"),
        (3.0 + 2.4, 5.0 + 2.4, "после"),
        (6.0 + 2.4, 8.0 + 2.4, "хвост"),
    ]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_studio_captions.py -x -q -k "insert_gap or shift"`
Expected: FAIL — `ImportError: cannot import name 'pick_insert_gap'`

- [ ] **Step 3: Implement** — append to `captions.py`:

```python
def pick_insert_gap(
    spans: list[tuple[float, float]], total: float,
) -> float | None:
    """Midpoint of the longest gap between speech spans whose midpoint
    falls within 20%–85% of total; None if that gap is < 0.5s. This is
    where the body take is split for cutaway inserts."""
    best: tuple[float, float] | None = None  # (length, midpoint)
    for (_, e1), (s2, _) in zip(spans, spans[1:]):
        length = s2 - e1
        mid = (e1 + s2) / 2
        if not (0.2 * total <= mid <= 0.85 * total):
            continue
        if best is None or length > best[0]:
            best = (length, mid)
    if best is None or best[0] < 0.5:
        return None
    return best[1]


def shift_captions(
    aligned: list[tuple[float, float, str]],
    split_at: float,
    inserts_seconds: float,
) -> list[tuple[float, float, str]]:
    """Shift sentences that start at/after the split right by the total
    insert duration (inserts are pushed into the timeline at split_at)."""
    return [
        (s + inserts_seconds, e + inserts_seconds, t) if s >= split_at
        else (s, e, t)
        for s, e, t in aligned
    ]
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_studio_captions.py -x -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/strategy_single_take/captions.py tests/test_studio_captions.py
git commit -m "feat(studio): pick_insert_gap + shift_captions for cutaway splicing"
```

---

### Task 4: assemble helpers — cut_clip, still_to_clip, silent-audio injection

**Files:**
- Modify: `app/services/strategy_single_take/assemble.py`
- Test: `tests/test_studio_captions.py` (pure parts only — ffmpeg funcs get arg-building tested via cmd builders)

The ffmpeg wrappers follow the existing style (`_run` + explicit arg lists). To keep them unit-testable without ffmpeg, each new op gets a pure `*_cmd` builder (pattern: `detect_silences_cmd`).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_studio_captions.py`:

```python
def test_cut_clip_cmd():
    from pathlib import Path
    from app.services.strategy_single_take.assemble import VF_NORMALIZE, cut_clip_cmd
    cmd = cut_clip_cmd(Path("in.mp4"), Path("out.mp4"), start=0.0, end=6.25)
    s = " ".join(cmd)
    assert "-ss 0.0" in s and "-to 6.25" in s
    assert VF_NORMALIZE in s          # re-encode keeps concat uniform
    # open-ended tail cut
    cmd2 = cut_clip_cmd(Path("in.mp4"), Path("out.mp4"), start=6.25)
    s2 = " ".join(cmd2)
    assert "-ss 6.25" in s2 and "-to" not in s2


def test_still_to_clip_cmd():
    from pathlib import Path
    from app.services.strategy_single_take.assemble import CUTAWAY_SECONDS, still_to_clip_cmd
    assert CUTAWAY_SECONDS == 1.2
    cmd = still_to_clip_cmd(Path("s.jpg"), Path("c.mp4"), seconds=1.2)
    s = " ".join(cmd)
    assert "-loop 1" in s
    assert "anullsrc" in s            # silent audio track
    assert "-t 1.2" in s


def test_normalize_clip_cmd_injects_silent_audio():
    from pathlib import Path
    from app.services.strategy_single_take.assemble import normalize_clip_cmd
    with_audio = " ".join(normalize_clip_cmd(Path("a.mp4"), Path("b.mp4"), has_audio=True))
    without = " ".join(normalize_clip_cmd(Path("a.mp4"), Path("b.mp4"), has_audio=False))
    assert "anullsrc" not in with_audio
    assert "anullsrc" in without      # Kling clips are silent
    assert "-shortest" in without
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_studio_captions.py -x -q -k "cut_clip or still_to_clip or injects"`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement.** In `assemble.py`:

Add after `VF_NORMALIZE`:

```python
CUTAWAY_SECONDS = 1.2
```

Add a stream-probe helper after `probe_duration`:

```python
def has_audio_stream(path: Path) -> bool:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return "audio" in r.stdout
```

Replace `normalize_clip` with cmd-builder + wrapper (silent sources get an injected `anullsrc` track so `concat_clips` stays valid):

```python
def normalize_clip_cmd(src: Path, dst: Path, *, has_audio: bool) -> list[str]:
    cmd = [FFMPEG, "-y", "-v", "error", "-i", str(src)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-map", "0:v", "-map", "1:a", "-shortest"]
    cmd += ["-vf", VF_NORMALIZE,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", str(dst)]
    return cmd


def normalize_clip(src: Path, dst: Path) -> Path:
    _run(normalize_clip_cmd(src, dst, has_audio=has_audio_stream(src)))
    return dst
```

Add after `concat_clips`:

```python
def cut_clip_cmd(
    src: Path, dst: Path, *, start: float, end: float | None = None,
) -> list[str]:
    cmd = [FFMPEG, "-y", "-v", "error", "-i", str(src), "-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    cmd += ["-vf", VF_NORMALIZE,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", str(dst)]
    return cmd


def cut_clip(src: Path, dst: Path, *, start: float, end: float | None = None) -> Path:
    _run(cut_clip_cmd(src, dst, start=start, end=end))
    return dst


def still_to_clip_cmd(jpg: Path, dst: Path, *, seconds: float) -> list[str]:
    return [FFMPEG, "-y", "-v", "error",
            "-loop", "1", "-framerate", "30", "-i", str(jpg),
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", str(seconds),
            "-vf", VF_NORMALIZE,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", str(dst)]


def still_to_clip(jpg: Path, dst: Path, *, seconds: float = CUTAWAY_SECONDS) -> Path:
    """Static fallback when Kling animation failed but the still exists."""
    _run(still_to_clip_cmd(jpg, dst, seconds=seconds))
    return dst
```

- [ ] **Step 4: Run the full suite** (normalize_clip signature unchanged, but verify nothing broke)

Run: `python -m pytest tests/ -x -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/strategy_single_take/assemble.py tests/test_studio_captions.py
git commit -m "feat(studio): cut_clip, still_to_clip, silent-audio normalize for cutaways"
```

---

### Task 5: script prompt — cutaways flag

**Files:**
- Modify: `app/services/strategy_single_take/script.py`
- Modify: `app/api/studio.py` (`ScriptRequest` + `make_script`)
- Test: `tests/test_studio_captions.py`, `tests/test_studio_service.py`

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_studio_captions.py`:

```python
def test_studio_script_prompt_cutaways_flag():
    from app.services.strategy_single_take.script import build_studio_script_prompt
    kw = dict(
        product_name="WHITE CHOCOLATE", brand="Richard Maison",
        price_rub=1990.0, dupe_price_rub=16000.0, voice_style="asmr",
    )
    p_off = build_studio_script_prompt(**kw, cutaways=False)
    assert "НЕ совершает действий" in p_off       # PR #68 full ban intact
    assert "обещани" in p_off.lower()

    p_on = build_studio_script_prompt(**kw, cutaways=True)
    assert "Сейчас открою" in p_on                # exactly one promise allowed
    assert "паузу" in p_on.lower()                # explicit long pause demanded
    assert "НЕ совершает действий" not in p_on
```

Update the existing `test_studio_script_prompt_asmr_vs_normal` calls in `tests/test_studio_captions.py:141-153` — `build_studio_script_prompt` gains a required kwarg; add `cutaways=False` to both calls (assertions unchanged).

Append to `tests/test_studio_service.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_studio_captions.py::test_studio_script_prompt_cutaways_flag tests/test_studio_service.py::test_api_script_autogen_passes_cutaways -x -q`
Expected: FAIL — unexpected keyword `cutaways`

- [ ] **Step 3: Implement.** In `script.py`, add `cutaways: bool` kwarg to both functions and branch the ban block. `build_studio_script_prompt` signature:

```python
def build_studio_script_prompt(
    *,
    product_name: str,
    brand: str,
    price_rub: float,
    dupe_price_rub: float,
    voice_style: str,
    cutaways: bool,
) -> str:
```

Replace the current ban sentence (lines 41-44) with:

```python
    if cutaways:
        actions = (
            "Часть 1 (до реакции) должна ЗАКАНЧИВАТЬСЯ ровно одной короткой "
            "фразой-обещанием действия («Сейчас открою…» или «Давайте "
            "попробуем…»), после которой идёт явная длинная пауза — в этот "
            "момент будет вставлен видеофрагмент с действием. Других "
            "обещаний действий («нанесу», «распылю», «покажу») быть не "
            "должно. Часть 2 — чистая реакция на запах, как уже "
            "случившееся впечатление.\n"
        )
    else:
        actions = (
            "В кадре только говорящая голова — героиня НЕ совершает действий. "
            "Запрещены обещания действий на камеру: «сейчас открою», «нанесу», "
            "«распылю», «покажу» и т.п. Про запах говори как про уже "
            "случившееся впечатление и ощущения.\n"
        )
```

…and use `f"{actions}"` in place of the old text inside the returned string. `generate_studio_script` gains `cutaways: bool` and passes it through to `build_studio_script_prompt`.

In `app/api/studio.py`: `ScriptRequest` gains `cutaways_enabled: bool = True`; `make_script` passes `cutaways=req.cutaways_enabled` to `generate_studio_script`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_studio_captions.py tests/test_studio_service.py -x -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/strategy_single_take/script.py app/api/studio.py tests/test_studio_captions.py tests/test_studio_service.py
git commit -m "feat(studio): script prompt promise-phrase mode for cutaways"
```

---

### Task 6: API — create flag, response fields, retry clearing

**Files:**
- Modify: `app/api/studio.py`
- Modify: `app/api/media.py`
- Test: `tests/test_studio_service.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_studio_service.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_studio_service.py -x -q -k cutaway`
Expected: FAIL — response lacks `cutaways_enabled` / keys survive retry / allowlist rejects

- [ ] **Step 3: Implement.** In `app/api/studio.py`:

`StudioJobResponse` — add fields after `captions_enabled: bool`:

```python
    cutaways_enabled: bool
```

and after `lipsync_key`:

```python
    cap_still_key: Optional[str]
    spray_still_key: Optional[str]
    cap_clip_key: Optional[str]
    spray_clip_key: Optional[str]
```

`from_model` — add correspondingly:

```python
            cutaways_enabled=bool(j.cutaways_enabled),
            cap_still_key=j.cap_still_key,
            spray_still_key=j.spray_still_key,
            cap_clip_key=j.cap_clip_key,
            spray_clip_key=j.spray_clip_key,
```

`create_job` — add param after `captions_enabled`:

```python
    cutaways_enabled: bool = Form(True),
```

and pass `cutaways_enabled=cutaways_enabled` to `create_studio_job_async`.

`retry_job` — after `j.lipsync_key = None` add:

```python
    j.cap_still_key = None
    j.spray_still_key = None
    j.cap_clip_key = None
    j.spray_clip_key = None
```

In `app/services/studio_service.py`: `create_studio_job_async` gains kwarg `cutaways_enabled: bool = True` (after `captions_enabled`) and passes `cutaways_enabled=cutaways_enabled` into the `StudioJob(...)` constructor.

In `app/api/media.py` `_verify_key_in_db`, extend the StudioJob filter (after `| (StudioJob.output_key == key)`):

```python
                      | (StudioJob.cap_still_key == key)
                      | (StudioJob.spray_still_key == key)
                      | (StudioJob.cap_clip_key == key)
                      | (StudioJob.spray_clip_key == key)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_studio_service.py -x -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/studio.py app/api/media.py app/services/studio_service.py tests/test_studio_service.py
git commit -m "feat(studio): cutaways flag through API, retry clearing, media allowlist"
```

---

### Task 7: worker CUTAWAYS stage

**Files:**
- Modify: `app/workers/studio_worker.py`
- Test: `tests/test_studio_service.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_studio_service.py`. A shared helper patches the three upstream stages exactly like `test_worker_happy_path`:

```python
def _patch_pre_cutaway_stages(monkeypatch, w):
    monkeypatch.setattr(w, "generate_studio_portrait", lambda **kw: (b"p", 0.15))
    monkeypatch.setattr(w, "generate_voiceover_v3", lambda **kw: b"a")
    monkeypatch.setattr(w, "generate_lipsync", lambda **kw: (b"v", 0.74))

    def fake_assemble(job, tmp, lipsync_path, voiceover_path, hook_path):
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_studio_service.py -x -q -k worker_cutaway`
Expected: FAIL — `AttributeError: generate_cutaway_still` on monkeypatch (not imported in worker)

- [ ] **Step 3: Implement.** In `app/workers/studio_worker.py`:

Imports — after the `judge_video` import add:

```python
from app.services.strategy_single_take.cutaways import (
    KINDS as CUTAWAY_KINDS,
    animate_cutaway,
    generate_cutaway_still,
)
```

New stage block in `process_job`, between the LIPSYNC block and `# --- ASSEMBLE + JUDGE ---`:

```python
    # --- CUTAWAYS (optional, non-blocking — a reel without inserts still ships) ---
    if j.cutaways_enabled:
        _mark(db, j, StudioStatus.CUTAWAYS)
        for kind in CUTAWAY_KINDS:  # ("cap_off", "spray")
            clip_attr = "cap_clip_key" if kind == "cap_off" else "spray_clip_key"
            still_attr = "cap_still_key" if kind == "cap_off" else "spray_still_key"
            if getattr(j, clip_attr):
                continue  # resume-safe
            try:
                if not getattr(j, still_attr):
                    result, cost = generate_cutaway_still(
                        portrait_bytes=_get_blob(r2, j.portrait_key),
                        product_bytes=product_bytes,
                        kind=kind,
                        product_name=j.product_name,
                        brand=j.brand,
                        replicate_api_key=replicate_key,
                    )
                    blob = _to_bytes(result, timeout=120)
                    key = (f"users/{j.user_id}/studio/{j.id}/"
                           f"cutaway-{kind}-{uuid.uuid4().hex[:6]}.jpg")
                    r2.upload_bytes(key, blob, content_type="image/jpeg")
                    setattr(j, still_attr, key)
                    _add_cost(db, j, cost)
                result, cost = animate_cutaway(
                    still_bytes=_get_blob(r2, getattr(j, still_attr)),
                    kind=kind,
                    replicate_api_key=replicate_key,
                )
                blob = _to_bytes(result, timeout=300)
                key = (f"users/{j.user_id}/studio/{j.id}/"
                       f"cutaway-{kind}-{uuid.uuid4().hex[:6]}.mp4")
                r2.upload_bytes(key, blob, content_type="video/mp4")
                setattr(j, clip_attr, key)
                _add_cost(db, j, cost)
            except ReplicateTransientError as e:
                log.warning("studio %s transient on cutaway %s: %s", j.id, kind, e)
                raise
            except Exception as e:
                # non-blocking: still (if any) stays for static fallback
                log.warning("studio %s cutaway %s failed (non-blocking): %s",
                            j.id, kind, e)
                db.rollback()
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_studio_service.py -x -q`
Expected: all PASS (including old worker tests — `_make_pending_job` default `cutaways_enabled` column default is True, but old tests patch nothing for cutaways… **see note**)

**Note:** old tests (`test_worker_happy_path` etc.) create jobs with `cutaways_enabled` defaulting to True, and the unpatched `generate_cutaway_still` would try a real Replicate call — but the stage catches every Exception non-blockingly, so they still pass (network error → warning → continue). To keep them hermetic, update `_make_pending_job`'s `fields` dict to include `cutaways_enabled=False` as the default (tests that want the stage pass `cutaways_enabled=True` via `**over`).

- [ ] **Step 5: Commit**

```bash
git add app/workers/studio_worker.py tests/test_studio_service.py
git commit -m "feat(studio): CUTAWAYS worker stage — resume-safe, non-blocking"
```

---

### Task 8: assemble splice flow in `_assemble`

**Files:**
- Modify: `app/workers/studio_worker.py` (`_assemble` + call site)
- Test: `tests/test_studio_service.py`

`_assemble` gains insert paths and does: split body at `pick_insert_gap`, concat `hook? + body_a + inserts + body_b`, shift captions by insert duration then hook offset. ffmpeg funcs are monkeypatched in tests; the test asserts orchestration order.

- [ ] **Step 1: Write the failing test** — append to `tests/test_studio_service.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_studio_service.py -x -q -k "assemble_splices or no_gap"`
Expected: FAIL — `_assemble() got an unexpected keyword argument 'insert_paths'`

- [ ] **Step 3: Implement.** In `studio_worker.py`:

Extend imports from assemble/captions:

```python
from app.services.strategy_single_take.assemble import (
    AssembleError,
    CUTAWAY_SECONDS,
    burn_captions,
    concat_clips,
    cut_clip,
    detect_silences,
    normalize_clip,
    polish,
    probe_duration,
    still_to_clip,
)
from app.services.strategy_single_take.captions import (
    SILENCE_ASMR,
    SILENCE_NORMAL,
    align_sentences,
    build_ass,
    parse_silencedetect,
    pick_insert_gap,
    shift_captions,
    speech_spans,
    split_sentences,
)
```

Replace `_assemble` entirely:

```python
def _assemble(
    j: StudioJob,
    tmp: Path,
    lipsync_path: Path,
    voiceover_path: Path,
    hook_path: Path | None,
    insert_paths: list[Path] | None = None,
) -> Path:
    """normalize → optional cutaway splice → optional hook concat →
    captions → polish. Returns final mp4."""
    body = normalize_clip(lipsync_path, tmp / "body.mp4")

    # VO analysis (needed for both splice point and captions)
    noise, min_d = SILENCE_ASMR if j.voice_style == "asmr" else SILENCE_NORMAL
    stderr = detect_silences(voiceover_path, noise=noise, min_d=min_d)
    vo_total = probe_duration(voiceover_path)
    spans = speech_spans(parse_silencedetect(stderr), total=vo_total)

    # --- cutaway splice (optional) ---
    split_at: float | None = None
    inserts_seconds = 0.0
    parts: list[Path] = [body]
    if insert_paths:
        split_at = pick_insert_gap(spans, vo_total)
    if split_at is not None:
        body_a = cut_clip(body, tmp / "body_a.mp4", start=0.0, end=split_at)
        body_b = cut_clip(body, tmp / "body_b.mp4", start=split_at)
        trimmed: list[Path] = []
        for i, ins in enumerate(insert_paths):
            ins_n = normalize_clip(ins, tmp / f"ins_n_{i}.mp4")
            trimmed.append(
                cut_clip(ins_n, tmp / f"ins_{i}.mp4",
                         start=0.0, end=CUTAWAY_SECONDS)
            )
        inserts_seconds = CUTAWAY_SECONDS * len(trimmed)
        parts = [body_a, *trimmed, body_b]

    hook_seconds = 0.0
    if hook_path is not None:
        hook_n = normalize_clip(hook_path, tmp / "hook_n.mp4")
        hook_seconds = probe_duration(hook_n)
        parts = [hook_n, *parts]

    raw = concat_clips(parts, tmp / "raw.mp4") if len(parts) > 1 else parts[0]

    staged = raw
    if j.captions_enabled and j.script_text:
        sents = split_sentences(j.script_text)
        aligned = align_sentences(sents, spans)
        # shift for inserts FIRST (split_at is in the VO timeline),
        # THEN into the final timeline (hook precedes the take)
        if split_at is not None and inserts_seconds > 0:
            aligned = shift_captions(aligned, split_at, inserts_seconds)
        aligned = [(s + hook_seconds, e + hook_seconds, t) for s, e, t in aligned]
        ass_path = tmp / "captions.ass"
        ass_path.write_text(build_ass(aligned))
        staged = burn_captions(raw, ass_path, tmp / "subbed.mp4")

    return polish(staged, tmp / "final.mp4", hook_seconds=hook_seconds)
```

Call-site change in `process_job` (ASSEMBLE block) — download insert clips (or build static fallback from a surviving still) before `_assemble`:

```python
        insert_paths: list[Path] = []
        if j.cutaways_enabled:
            for kind, clip_attr, still_attr in (
                ("cap_off", "cap_clip_key", "cap_still_key"),
                ("spray", "spray_clip_key", "spray_still_key"),
            ):
                clip_key = getattr(j, clip_attr)
                still_key = getattr(j, still_attr)
                try:
                    if clip_key:
                        p = tmp / f"cut_{kind}.mp4"
                        p.write_bytes(_get_blob(r2, clip_key))
                        insert_paths.append(p)
                    elif still_key:
                        jpg = tmp / f"cut_{kind}.jpg"
                        jpg.write_bytes(_get_blob(r2, still_key))
                        insert_paths.append(
                            still_to_clip(jpg, tmp / f"cut_{kind}_static.mp4")
                        )
                except Exception as e:
                    log.warning("studio %s insert %s unusable (skip): %s",
                                j.id, kind, e)

        try:
            final_path = _assemble(
                j, tmp, lipsync_path, voiceover_path, hook_path,
                insert_paths=insert_paths or None,
            )
```

(the `except AssembleError` / `except Exception` handlers stay as-is).

Note: `_assemble` now always runs silencedetect (previously only with captions on) — acceptable, it's a cheap local ffmpeg pass; guard stays simple.

- [ ] **Step 4: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: all PASS (old `fake_assemble` monkeypatches take `hook_path` positionally — new kwarg `insert_paths` is passed by keyword, so update `fake_assemble` signatures in old tests to `def fake_assemble(job, tmp, lipsync_path, voiceover_path, hook_path, insert_paths=None):`)

- [ ] **Step 5: Commit**

```bash
git add app/workers/studio_worker.py tests/test_studio_service.py
git commit -m "feat(studio): splice cutaway inserts into body at VO pause, shift captions"
```

---

### Task 9: UI — checkbox + stage chip

**Files:**
- Modify: `static/studio.html`

No JS unit tests in this repo — verify by serving locally + code review.

- [ ] **Step 1: Add checkbox** after the `f-captions` label (line ~71):

```html
    <label class="text-sm flex items-center gap-2">
      <input id="f-cutaways" type="checkbox" checked> вставки-действия (открыть + пшик)
    </label>
```

- [ ] **Step 2: Wire it into both requests.**

In `genScript()` body JSON add:

```js
        cutaways_enabled: document.getElementById('f-cutaways').checked,
```

In the job-create `FormData` block after `captions_enabled`:

```js
  fd.append('cutaways_enabled', document.getElementById('f-cutaways').checked);
```

- [ ] **Step 3: Stage chip.** In `STAGES` insert between lipsync and assemble:

```js
  ['lipsync',   'Липсинк'],
  ['cutaways',  'Вставки'],
  ['assemble',  'Сборка'],
```

In `ORDER` insert `'cutaways'` between `'lipsync'` and `'assemble'`:

```js
const ORDER = ['pending','portrait','voiceover','lipsync','cutaways','assemble','judge','ready'];
```

The chips key-presence check uses `j[key + '_key']` — `cutaways_key` doesn't exist, so extend the failed-state line to treat cutaways via `cap_clip_key`:

```js
      cls = kidx < idx || (j[key + '_key'] || (key === 'judge' && j.judge_score != null) || (key === 'cutaways' && j.cap_clip_key))
        ? 'chip chip-done' : 'chip chip-fail';
```

- [ ] **Step 4: Smoke-check** — `cd /opt/projects/reelstracker-saas && python -c "import pathlib; t = pathlib.Path('static/studio.html').read_text(); assert 'f-cutaways' in t and t.count('cutaways_enabled') >= 2 and \"'cutaways'\" in t"`

- [ ] **Step 5: Commit**

```bash
git add static/studio.html
git commit -m "feat(studio): cutaways checkbox + stage chip in UI"
```

---

### Task 10: full suite + PR

- [ ] **Step 1: Full test run**

Run: `python -m pytest tests/ -q`
Expected: 0 failures (65 pre-existing + ~15 new)

- [ ] **Step 2: Push + PR**

```bash
git push -u origin feat/studio-cutaways
gh pr create --title "feat(studio): action cutaway inserts (cap-off + spray)" --body "..."
```

PR body: summary of the pipeline change, cost delta (+~$0.80 worst case), link to spec, test plan (merge → redeploy → prod reel with cutaways_enabled=true → judge + eyeball → report actual Kling std cost).

---

## Post-merge (not part of the code plan, per session workflow)

1. `gh pr merge --squash --delete-branch`, Railway auto-deploy (verify via `railway deployment list --json`).
2. Prod E2E: reuse `/tmp/studio_first_reel.py` pattern with `cutaways_enabled=true`; product photo `/opt/vault/Areas/MIMIC/products/dose_white_chocolate_real_hand.jpg`, hook `/opt/vault/Projects/ideas/_assets/wc_single_take/hook_norm.mp4`.
3. Log actual costs (`log_cost.py ideas replicate ...`, `eleven-tts`), note real Kling std price vs the $0.25 estimate.
4. Send video + short report to Nick in TG (own token from `/opt/tg-bot-ideas/.env`, `--data-urlencode`).
5. Update memory `feedback_studio_script_no_actions.md` (rule can now be relaxed when cutaways enabled) + `project_studio_poc.md`.
