# Studio Single-Take UGC POC — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Productize the vault WC single-take pipeline (v31–v36) as a `/studio` page in reelstracker-saas: form in → finished reel out with stage progress and an auto-QC score.

**Architecture:** New `studio_jobs` table + polling worker thread (same pattern as `makeugc_worker`), new pipeline package `app/services/strategy_single_take/` (portrait via nano-banana-pro, eleven_v3 voiceover with ASMR whisper tags, Pruna lipsync reused from `strategy_makeugc`, ffmpeg assemble + word-by-word ASS captions + polish, Gemini video judge), REST API under `/api/studio`, glassmorphism SPA page.

**Tech Stack:** FastAPI, SQLAlchemy 2, Replicate SDK (via existing `ReplicateClient`), ElevenLabs HTTP API, Gemini REST API (`requests`), ffmpeg CLI, Tailwind CDN static page.

**Spec:** `docs/specs/2026-07-06-studio-single-take-design.md` (approved by Nick 2026-07-06). Branch: `feat/studio-single-take`.

**Reference recipes (read-only, do not modify):** `/opt/vault/Projects/ideas/_assets/wc_single_take/build_v36.py` (captions + polish), `tts_tail_dose.py` (eleven_v3 settings), `/opt/tg-bot/tools/reel_judge.py` (judge rubric).

**Run tests with:** `cd /opt/projects/reelstracker-saas && python3 -m pytest tests/<file> -v` (SQLite in-memory fallback; no TEST_DATABASE_URL needed).

---

## File Structure

| File | Responsibility |
|---|---|
| `app/models/studio_job.py` (new) | `StudioJob` row + `StudioStatus` enum |
| `app/models/__init__.py` (modify) | register model for `create_all` |
| `app/services/strategy_single_take/__init__.py` (new) | empty package marker |
| `app/services/strategy_single_take/captions.py` (new) | silencedetect parser, sentence alignment, word-by-word ASS generator |
| `app/services/strategy_single_take/voiceover.py` (new) | eleven_v3 TTS + `[whispers]` tag injection |
| `app/services/strategy_single_take/portrait.py` (new) | nano-banana-pro portrait with product photo + misspelling ban-list |
| `app/services/strategy_single_take/script.py` (new) | script autogen (ASMR-aware prompt, reuses `_format_rub`/`_clean_brand`) |
| `app/services/strategy_single_take/assemble.py` (new) | ffmpeg: normalize / concat / burn ASS / polish |
| `app/services/strategy_single_take/judge.py` (new) | Gemini video QC with model fallback rotation |
| `app/services/studio_service.py` (new) | job create: validation + R2 upload + PENDING row |
| `app/workers/studio_worker.py` (new) | polling drain loop, stage orchestration |
| `app/api/studio.py` (new) | REST endpoints |
| `app/main.py` (modify) | router mount, `/studio` route, worker thread |
| `static/studio.html` (new) | UI |
| `tests/test_studio_captions.py` (new) | parser + alignment + ASS math |
| `tests/test_studio_judge.py` (new) | fallback rotation (mocked HTTP) |
| `tests/test_studio_service.py` (new) | create-job validation + worker orchestration (mocked stages) |

Existing modules reused as-is (import, don't copy): `app.services.strategy_makeugc.lipsync.generate_lipsync`, `app.services.strategy_makeugc.voiceover.resolve_api_key/resolve_voice_id`, `app.services.strategy_makeugc.script._format_rub/_clean_brand`, `app.services.makeugc_service.ALLOWED_PRODUCT_CONTENT_TYPES/MAX_PRODUCT_IMAGE_BYTES/MAX_PRODUCT_IMAGES/MAX_BROLL_BYTES/ALLOWED_BROLL_CONTENT_TYPES`, `app.core.storage.get_r2`, `app.services.media_helpers.download_bytes`, `app.services.replicate_client.ReplicateClient`.

---

### Task 1: StudioJob model

**Files:**
- Create: `app/models/studio_job.py`
- Modify: `app/models/__init__.py`
- Test: `tests/test_studio_service.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_studio_service.py`:

```python
"""Studio POC — service + worker orchestration tests."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest


def test_studio_job_roundtrip(db_session, test_user):
    from app.models.studio_job import StudioJob, StudioStatus

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_studio_service.py::test_studio_job_roundtrip -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.studio_job'`

- [ ] **Step 3: Write the model**

Create `app/models/studio_job.py`:

```python
"""StudioJob — async job row for the single-take UGC Studio (POC).

Lifecycle:
  PENDING → PORTRAIT → VOICEOVER → LIPSYNC → ASSEMBLE → JUDGE → READY
                                                               ↘ FAILED

Judge is non-blocking: a judge failure still lands the job in READY
with judge_score NULL (UI shows «QC недоступен»).
"""
from __future__ import annotations

import enum

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON,
    Numeric, String, Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


class StudioStatus(str, enum.Enum):
    PENDING = "pending"
    PORTRAIT = "portrait"
    VOICEOVER = "voiceover"
    LIPSYNC = "lipsync"
    ASSEMBLE = "assemble"
    JUDGE = "judge"
    READY = "ready"
    FAILED = "failed"


class StudioJob(Base):
    __tablename__ = "studio_jobs"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )

    # Inputs
    product_image_keys = Column(JSON, nullable=False, default=list)
    product_name = Column(String(128), nullable=False)
    brand = Column(String(128), nullable=False)
    price_rub = Column(Numeric(10, 2), nullable=False)
    dupe_price_rub = Column(Numeric(10, 2), nullable=False)
    script_text = Column(Text, nullable=True)
    voice_style = Column(String(16), nullable=False, default="normal")  # normal|asmr
    captions_enabled = Column(Boolean, nullable=False, default=True)
    hook_video_key = Column(Text, nullable=True)

    # Stage outputs
    portrait_key = Column(Text, nullable=True)
    voiceover_key = Column(Text, nullable=True)
    lipsync_key = Column(Text, nullable=True)
    output_key = Column(Text, nullable=True)
    judge_score = Column(Integer, nullable=True)
    judge_report = Column(JSON, nullable=True)

    status = Column(String(24), nullable=False, default=StudioStatus.PENDING)
    error_message = Column(Text, nullable=True)
    cost_usd = Column(Numeric(8, 4), nullable=False, default=0)

    created_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_studio_jobs_user_id", "user_id"),
        Index("ix_studio_jobs_status", "status"),
    )

    user = relationship("User")
```

- [ ] **Step 4: Register in `app/models/__init__.py`**

Add after the `MakeUGCJob` import line:

```python
from app.models.studio_job import StudioJob, StudioStatus
```

and append `"StudioJob", "StudioStatus",` to `__all__`.

Note: the table is created via `Base.metadata.create_all` in `app/main.py` lifespan (line ~403) — it's a brand-new table, so no entries in the `run_lightweight_migrations()` ALTER list are needed.

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_studio_service.py::test_studio_job_roundtrip -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/models/studio_job.py app/models/__init__.py tests/test_studio_service.py
git commit -m "feat(studio): StudioJob model + status enum"
```

### Task 2: captions.py — silencedetect parser, sentence alignment, ASS generator

Port of the caption logic in `/opt/vault/Projects/ideas/_assets/wc_single_take/build_v36.py` (lines 35–82), generalized: sentence spans come from ffmpeg `silencedetect` on the voiceover instead of hand-tuned constants.

**Files:**
- Create: `app/services/strategy_single_take/__init__.py` (empty file)
- Create: `app/services/strategy_single_take/captions.py`
- Test: `tests/test_studio_captions.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_studio_captions.py`:

```python
"""Unit tests for the Studio caption pipeline (pure math, no ffmpeg)."""
from __future__ import annotations

import pytest

from app.services.strategy_single_take.captions import (
    align_sentences,
    build_ass,
    parse_silencedetect,
    speech_spans,
    split_sentences,
    _ts,
)


FFMPEG_STDERR = """\
[silencedetect @ 0x5555] silence_start: 1.10
[silencedetect @ 0x5555] silence_end: 1.60 | silence_duration: 0.50
[silencedetect @ 0x5555] silence_start: 6.20
[silencedetect @ 0x5555] silence_end: 6.50 | silence_duration: 0.30
size=N/A time=00:00:10.00 bitrate=N/A speed= 500x
"""


def test_parse_silencedetect():
    assert parse_silencedetect(FFMPEG_STDERR) == [(1.10, 1.60), (6.20, 6.50)]


def test_parse_silencedetect_unclosed_final_silence():
    # trailing silence with no silence_end (audio ends silent)
    stderr = "[x] silence_start: 8.0\n"
    assert parse_silencedetect(stderr) == [(8.0, None)]


def test_speech_spans_inverts_silences():
    spans = speech_spans([(1.10, 1.60), (6.20, 6.50)], total=10.0)
    assert spans == [(0.0, 1.10), (1.60, 6.20), (6.50, 10.0)]


def test_speech_spans_trailing_silence():
    spans = speech_spans([(8.0, None)], total=10.0)
    assert spans == [(0.0, 8.0)]


def test_speech_spans_no_silence():
    assert speech_spans([], total=10.0) == [(0.0, 10.0)]


def test_split_sentences():
    text = "Я это заказала. Первый раз в жизни! Ну что?"
    assert split_sentences(text) == [
        "Я это заказала", "Первый раз в жизни", "Ну что?",
    ]


def test_split_sentences_strips_eleven_tags():
    text = "[whispers] Только не за 16 тысяч. [curious] Ну что?"
    assert split_sentences(text) == ["Только не за 16 тысяч", "Ну что?"]


def test_align_sentences_exact_match():
    sents = ["раз", "два"]
    spans = [(0.0, 1.0), (2.0, 3.0)]
    assert align_sentences(sents, spans) == [
        (0.0, 1.0, "раз"), (2.0, 3.0, "два"),
    ]


def test_align_sentences_mismatch_falls_back_to_proportional():
    # 3 sentences, 2 speech spans → distribute by char length over
    # [first_start, last_end] with a small gap between sentences.
    sents = ["ab", "ab", "ab"]
    spans = [(0.0, 4.0), (5.0, 9.0)]
    out = align_sentences(sents, spans)
    assert len(out) == 3
    assert out[0][0] == 0.0
    assert out[-1][1] == pytest.approx(9.0, abs=0.01)
    # monotonic, non-overlapping
    for (s1, e1, _), (s2, e2, _) in zip(out, out[1:]):
        assert e1 <= s2
        assert s1 < e1 and s2 < e2


def test_ts_format():
    assert _ts(0.0) == "0:00:00.00"
    assert _ts(83.5) == "0:01:23.50"


def test_build_ass_word_timing_proportional():
    ass = build_ass([(0.0, 3.0, "яя ббб")])
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(lines) == 2
    # weights: len+1 → 3 and 4; word1 gets 3/7 of 3.0s ≈ 1.29
    assert lines[0].endswith(",яя")
    assert lines[1].endswith(",ббб")
    assert "0:00:00.00,0:00:01.29" in lines[0]
    assert "0:00:01.29,0:00:03.00" in lines[1]


def test_build_ass_header_style():
    ass = build_ass([(0.0, 1.0, "слово")])
    assert "PlayResX: 720" in ass
    assert "PlayResY: 1280" in ass
    # v36 style: DejaVu Sans 58, Alignment 2, MarginV 215
    assert "Style: cap,DejaVu Sans,58," in ass
    style_line = next(l for l in ass.splitlines() if l.startswith("Style:"))
    fields = style_line.split(",")
    assert fields[18] == "2"    # Alignment
    assert fields[21] == "215"  # MarginV
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_studio_captions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.strategy_single_take'`

- [ ] **Step 3: Implement**

Create empty `app/services/strategy_single_take/__init__.py`, then `app/services/strategy_single_take/captions.py`:

```python
"""Word-by-word ASS captions for the single-take pipeline.

Recipe from vault wc_single_take v35/v36: per-word duration is
proportional to len(word)+1 across each sentence span; sentence spans
come from ffmpeg silencedetect over the voiceover track. Whisper audio
has a high noise floor — use -26dB/d=0.07 for ASMR, -25dB/d=0.12 for
normal voice (thresholds validated on v36, 2026-07-06).
"""
from __future__ import annotations

import re


SILENCE_ASMR = ("-26dB", 0.07)
SILENCE_NORMAL = ("-25dB", 0.12)

_START_RE = re.compile(r"silence_start:\s*([\d.]+)")
_END_RE = re.compile(r"silence_end:\s*([\d.]+)")
_TAG_RE = re.compile(r"\[[a-z ]+\]\s*", re.IGNORECASE)  # eleven_v3 audio tags

ASS_HEADER = """[Script Info]
PlayResX: 720
PlayResY: 1280
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: cap,DejaVu Sans,58,&H00FFFFFF,&H00FFFFFF,&H96000000,&H96000000,-1,0,0,0,100,100,0,0,1,2,1,2,40,40,215,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def parse_silencedetect(stderr: str) -> list[tuple[float, float | None]]:
    """(start, end) pairs from ffmpeg silencedetect stderr.
    A trailing silence with no matching end yields (start, None)."""
    out: list[tuple[float, float | None]] = []
    pending: float | None = None
    for line in stderr.splitlines():
        m = _START_RE.search(line)
        if m:
            pending = float(m.group(1))
            continue
        m = _END_RE.search(line)
        if m and pending is not None:
            out.append((pending, float(m.group(1))))
            pending = None
    if pending is not None:
        out.append((pending, None))
    return out


def speech_spans(
    silences: list[tuple[float, float | None]], total: float,
) -> list[tuple[float, float]]:
    """Invert silence intervals into speech intervals over [0, total]."""
    spans: list[tuple[float, float]] = []
    cur = 0.0
    for start, end in silences:
        if start - cur > 0.01:
            spans.append((cur, start))
        if end is None:
            return spans
        cur = end
    if total - cur > 0.01:
        spans.append((cur, total))
    return spans


def split_sentences(text: str) -> list[str]:
    """Split script into sentences; strips eleven_v3 [tags]. Keeps '?'
    (question intonation reads better on screen), drops '.'/'!'/'…'."""
    text = _TAG_RE.sub("", text or "")
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    out = []
    for p in parts:
        p = p.strip().rstrip(".!…").strip()
        if p:
            out.append(p)
    return out


def align_sentences(
    sentences: list[str], spans: list[tuple[float, float]],
) -> list[tuple[float, float, str]]:
    """Map sentences onto speech spans. Exact count match → zip;
    otherwise distribute proportionally to char length over the full
    voiced window with a fixed 0.25s inter-sentence gap."""
    if not sentences:
        return []
    if spans and len(spans) == len(sentences):
        return [(s, e, txt) for (s, e), txt in zip(spans, sentences)]

    lo = spans[0][0] if spans else 0.0
    hi = spans[-1][1] if spans else 0.0
    gap = 0.25
    window = max(hi - lo - gap * (len(sentences) - 1), 0.1)
    weights = [max(len(s), 1) for s in sentences]
    total_w = sum(weights)
    out: list[tuple[float, float, str]] = []
    t = lo
    for txt, w in zip(sentences, weights):
        d = window * w / total_w
        out.append((t, t + d, txt))
        t += d + gap
    return out


def _ts(t: float) -> str:
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(sent_spans: list[tuple[float, float, str]]) -> str:
    """Word-by-word Dialogue lines, per-word duration ∝ len(word)+1."""
    lines: list[str] = []
    for start, end, text in sent_spans:
        words = text.split()
        weights = [len(w) + 1 for w in words]
        total = sum(weights)
        t = start
        for w, wt in zip(words, weights):
            d = (end - start) * wt / total
            lines.append(
                f"Dialogue: 0,{_ts(t)},{_ts(t + d)},cap,,0,0,0,,{w}"
            )
            t += d
    return ASS_HEADER + "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_studio_captions.py -v`
Expected: all PASS. If `test_build_ass_word_timing_proportional` fails on the exact `1.29` timestamp, check rounding in `_ts` (must be `f"{s:05.2f}"`, truncation not allowed).

- [ ] **Step 5: Commit**

```bash
git add app/services/strategy_single_take/ tests/test_studio_captions.py
git commit -m "feat(studio): captions module — silencedetect parser + word-by-word ASS"
```

### Task 3: voiceover.py — eleven_v3 + ASMR whisper tags

`strategy_makeugc.voiceover` is pinned to `eleven_multilingual_v2` with different settings, so Studio gets its own thin TTS function (settings from vault `tts_tail_dose.py`, validated on v31–v36). Key/voice resolution is reused from makeugc.

**Files:**
- Create: `app/services/strategy_single_take/voiceover.py`
- Test: `tests/test_studio_captions.py` (append — it's the pure-function test file)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_studio_captions.py`:

```python
def test_apply_asmr_tags():
    from app.services.strategy_single_take.voiceover import apply_asmr_tags
    text = "Я это заказала. Ну что?"
    assert apply_asmr_tags(text) == "[whispers] Я это заказала. [whispers] Ну что?"


def test_apply_asmr_tags_idempotent_on_tagged_text():
    from app.services.strategy_single_take.voiceover import apply_asmr_tags
    text = "[whispers] Уже с тегом."
    assert apply_asmr_tags(text) == "[whispers] Уже с тегом."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_studio_captions.py -k asmr -v`
Expected: FAIL with `ModuleNotFoundError` / `ImportError`

- [ ] **Step 3: Implement**

Create `app/services/strategy_single_take/voiceover.py`:

```python
"""Studio TTS — eleven_v3 with the vault-validated whisper settings.

stability 0.30 / style 0.85 / similarity 0.85 were picked on the WC
single-take iterations (v31–v36); eleven_v3 honors inline audio tags
like [whispers], which is how the ASMR voice style is produced.
Do NOT send language_code — eleven_v3 rejects it.
"""
from __future__ import annotations

import json
import re
import urllib.request

from app.services.strategy_makeugc.voiceover import VoiceoverError


TTS_MODEL_V3 = "eleven_v3"

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def apply_asmr_tags(text: str) -> str:
    """Prefix each sentence with [whispers] unless it already carries a tag."""
    parts = _SENT_SPLIT_RE.split(text.strip())
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p.startswith("["):
            out.append(p)
        else:
            out.append(f"[whispers] {p}")
    return " ".join(out)


def generate_voiceover_v3(
    *,
    script_text: str,
    voice_id: str,
    api_key: str,
    asmr: bool,
) -> bytes:
    """Run ElevenLabs eleven_v3 TTS, return raw MP3 bytes."""
    if not voice_id:
        raise VoiceoverError("voice_id is empty")
    if not api_key:
        raise VoiceoverError("api_key is empty")
    if not script_text or not script_text.strip():
        raise VoiceoverError("script_text is empty")

    text = apply_asmr_tags(script_text) if asmr else script_text
    body = {
        "text": text,
        "model_id": TTS_MODEL_V3,
        "voice_settings": {
            "stability": 0.30,
            "style": 0.85,
            "similarity_boost": 0.85,
            "use_speaker_boost": True,
        },
    }
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:400]
        raise VoiceoverError(f"ElevenLabs HTTP {e.code}: {body_text}") from e
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_studio_captions.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/strategy_single_take/voiceover.py tests/test_studio_captions.py
git commit -m "feat(studio): eleven_v3 voiceover with ASMR whisper tags"
```

---

### Task 4: portrait.py — nano-banana-pro with product photo + ban-list

**Files:**
- Create: `app/services/strategy_single_take/portrait.py`
- Test: `tests/test_studio_captions.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_studio_captions.py`:

```python
def test_studio_portrait_prompt_contains_banlist_and_framing():
    from app.services.strategy_single_take.portrait import build_studio_prompt
    p_asmr = build_studio_prompt(
        product_name="WHITE CHOCOLATE", brand="dose", asmr=True,
    )
    assert "WHITE CHOCOLATE" in p_asmr
    assert "dose" in p_asmr
    assert "misspell" in p_asmr.lower()
    assert "microphone" in p_asmr.lower()  # ASMR mic prop

    p_norm = build_studio_prompt(
        product_name="WHITE CHOCOLATE", brand="dose", asmr=False,
    )
    assert "microphone" not in p_norm.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_studio_captions.py -k banlist -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Create `app/services/strategy_single_take/portrait.py`:

```python
"""Studio portrait — google/nano-banana-pro on Replicate.

The real product photo goes in as image_input so the label survives;
the prompt carries an explicit misspelling ban-list (nano-banana
otherwise invents plausible-looking gibberish in fine print). Rule
from vault v11 iterations: never run more than 2 label-edit passes —
the 3rd degrades fine print. POC does a single pass.
"""
from __future__ import annotations

import base64

from app.services.replicate_client import ReplicateClient
from app.services.strategy_makeugc.portrait import _extract_output


MODEL_NANO = "google/nano-banana-pro"
COST_PER_IMAGE_USD = 0.15

_FRAMING_ASMR = (
    "Extreme close-up vertical portrait: an average European girl, "
    "23-27, leans toward a large studio condenser microphone with a pop "
    "filter, whisper-review ASMR setting, dim cozy bedroom light. She "
    "holds the product bottle right next to her face so the label faces "
    "the camera."
)
_FRAMING_NORMAL = (
    "Vertical UGC selfie portrait: an average European girl, 23-27, "
    "plain face, natural skin, sitting in her bedroom with soft window "
    "light, holding the product bottle in one hand near her face, label "
    "facing the camera, genuine slight smile."
)


def build_studio_prompt(*, product_name: str, brand: str, asmr: bool) -> str:
    framing = _FRAMING_ASMR if asmr else _FRAMING_NORMAL
    return (
        f"{framing} The bottle is the exact product from the reference "
        f"photo — keep its shape, cap, color and label identical. The "
        f"label text must read exactly «{brand}» and «{product_name}». "
        f"Do not misspell, redraw, translate or invent ANY text on the "
        f"label or anywhere in frame; if a word is not clearly legible "
        f"in the reference photo, keep it blurred rather than guessing. "
        f"Photorealistic, shot on a phone, shallow depth of field, "
        f"9:16 composition."
    )


def generate_studio_portrait(
    *,
    product_image_bytes: bytes,
    product_content_type: str,
    product_name: str,
    brand: str,
    asmr: bool,
    replicate_api_key: str,
) -> tuple[bytes | str, float]:
    """Run nano-banana-pro; return (bytes-or-url, cost_usd)."""
    uri = (
        f"data:{product_content_type};base64,"
        f"{base64.b64encode(product_image_bytes).decode()}"
    )
    params = {
        "prompt": build_studio_prompt(
            product_name=product_name, brand=brand, asmr=asmr,
        ),
        "image_input": [uri],
        "aspect_ratio": "9:16",
    }
    client = ReplicateClient(api_key=replicate_api_key)
    out = client.run_model(MODEL_NANO, params)
    return _extract_output(out), COST_PER_IMAGE_USD
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_studio_captions.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/strategy_single_take/portrait.py tests/test_studio_captions.py
git commit -m "feat(studio): nano-banana-pro portrait with label ban-list"
```

---

### Task 5: script.py — ASMR-aware script autogen

**Files:**
- Create: `app/services/strategy_single_take/script.py`
- Test: `tests/test_studio_captions.py` (append — prompt builder only, no OpenAI call in tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_studio_captions.py`:

```python
def test_studio_script_prompt_asmr_vs_normal():
    from app.services.strategy_single_take.script import build_studio_script_prompt
    p = build_studio_script_prompt(
        product_name="WHITE CHOCOLATE", brand="Richard Maison",
        price_rub=1990.0, dupe_price_rub=16000.0, voice_style="asmr",
    )
    assert "шёпот" in p.lower()
    assert "тысяча девятьсот девяносто рублей" in p
    p2 = build_studio_script_prompt(
        product_name="WHITE CHOCOLATE", brand="Richard Maison",
        price_rub=1990.0, dupe_price_rub=16000.0, voice_style="normal",
    )
    assert "шёпот" not in p2.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_studio_captions.py -k script_prompt -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Create `app/services/strategy_single_take/script.py`:

```python
"""Single-take script autogen. Reuses the ruble-words helpers from
strategy_makeugc.script; the prompt targets one continuous ~30s take
(the v35/v36 narrative arc: заказала вслепую → что это аналог → цена →
реакция), optionally in ASMR whisper register."""
from __future__ import annotations

import json
import urllib.request

from app.services.strategy_makeugc.script import (
    SCRIPT_MODEL,
    _clean_brand,
    _format_rub,
)


def build_studio_script_prompt(
    *,
    product_name: str,
    brand: str,
    price_rub: float,
    dupe_price_rub: float,
    voice_style: str,
) -> str:
    tone = (
        "Регистр: интимный шёпот-ASMR, короткие фразы, паузы, как будто "
        "рассказывает секрет на ухо."
        if voice_style == "asmr"
        else "Регистр: живо и эмоционально, как рассказывает подруге."
    )
    return (
        "Напиши сценарий озвучки для вертикального UGC-рилса, один "
        "непрерывный дубль на ~30 секунд (55-70 слов), на русском.\n"
        f"Продукт: аналог {_clean_brand(brand)} {product_name}.\n"
        f"Цена оригинала: {_format_rub(dupe_price_rub)}. "
        f"Цена аналога: {_format_rub(price_rub)} — цены произносить "
        "СЛОВАМИ, как написано.\n"
        "Арка: 1) заказала вслепую по чужой реакции, 2) что это аналог "
        "дорогого аромата, 3) цена-контраст, 4) живая реакция на запах.\n"
        f"{tone}\n"
        "Без хэштегов, без эмодзи, без ремарок в скобках — только текст "
        "который будет произнесён."
    )


def generate_studio_script(
    *,
    product_name: str,
    brand: str,
    price_rub: float,
    dupe_price_rub: float,
    voice_style: str,
    openai_api_key: str,
) -> str:
    prompt = build_studio_script_prompt(
        product_name=product_name, brand=brand,
        price_rub=price_rub, dupe_price_rub=dupe_price_rub,
        voice_style=voice_style,
    )
    body = {
        "model": SCRIPT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.9,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {openai_api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"].strip()
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_studio_captions.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/strategy_single_take/script.py tests/test_studio_captions.py
git commit -m "feat(studio): ASMR-aware script autogen"
```

### Task 6: assemble.py — ffmpeg normalize / concat / burn / polish

Port of `build_v36.py` ffmpeg steps, minus the gesture inserts (out of scope in POC). Uses bare `ffmpeg` binary name — same convention as `strategy_makeugc/concat.py:16` (present in the Railway image). The polish filter is a pure string builder so it's unit-testable without ffmpeg.

**Files:**
- Create: `app/services/strategy_single_take/assemble.py`
- Test: `tests/test_studio_captions.py` (append — filter builder only)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_studio_captions.py`:

```python
def test_polish_filter_hook_untouched_body_sped_up():
    from app.services.strategy_single_take.assemble import build_polish_filter
    fc = build_polish_filter(hook_seconds=3.204)
    assert "trim=0:3.204" in fc
    assert "setpts=(PTS-STARTPTS)/1.05" in fc
    assert "noise=alls=5:allf=t" in fc
    assert "atempo=1.05" in fc
    assert "concat=n=2:v=1:a=1" in fc


def test_polish_filter_no_hook():
    from app.services.strategy_single_take.assemble import build_polish_filter
    fc = build_polish_filter(hook_seconds=0.0)
    assert "trim=0:" not in fc      # no hook split
    assert "concat" not in fc       # single chain
    assert "setpts=(PTS-STARTPTS)/1.05" in fc
    assert "noise=alls=5:allf=t" in fc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_studio_captions.py -k polish -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Create `app/services/strategy_single_take/assemble.py`:

```python
"""ffmpeg assembly for the single-take reel.

Recipe = vault build_v36.py without gesture inserts:
  normalize (720×1280 crop-fill, 30fps) → optional hook concat →
  burn ASS captions → polish (body ×1.05 + film grain, hook untouched,
  NO handheld shake — Nick's UGC-style rule: рилсы снимают со штатива).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# crop-fill (not pad): matches the vault recipe — talking head fills frame
VF_NORMALIZE = (
    "scale=720:1280:force_original_aspect_ratio=increase,"
    "crop=720:1280,fps=30"
)


class AssembleError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssembleError(
            f"ffmpeg failed (rc={r.returncode}): {r.stderr[-800:]}"
        )


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise AssembleError(f"ffprobe failed: {r.stderr[-300:]}")
    return float(r.stdout.strip())


def normalize_clip(src: Path, dst: Path) -> Path:
    _run([FFMPEG, "-y", "-v", "error", "-i", str(src),
          "-vf", VF_NORMALIZE,
          "-c:v", "libx264", "-preset", "fast", "-crf", "20",
          "-c:a", "aac", "-ar", "44100", "-ac", "2", str(dst)])
    return dst


def concat_clips(parts: list[Path], dst: Path) -> Path:
    """Concat demuxer over already-normalized parts (same codec/fps)."""
    lst = dst.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    _run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
          "-i", str(lst),
          "-c:v", "libx264", "-preset", "fast", "-crf", "20",
          "-c:a", "aac", "-ar", "44100", "-ac", "2", str(dst)])
    return dst


def burn_captions(src: Path, ass_path: Path, dst: Path) -> Path:
    _run([FFMPEG, "-y", "-v", "error", "-i", str(src),
          "-vf", f"ass={ass_path}",
          "-c:v", "libx264", "-preset", "fast", "-crf", "20",
          "-c:a", "copy", str(dst)])
    return dst


def build_polish_filter(*, hook_seconds: float) -> str:
    """Body ×1.05 + grain; hook (if any) passes through untouched."""
    if hook_seconds <= 0:
        return (
            "[0:v]setpts=(PTS-STARTPTS)/1.05,noise=alls=5:allf=t[v];"
            "[0:a]asetpts=PTS-STARTPTS,atempo=1.05[a]"
        )
    h = hook_seconds
    return (
        f"[0:v]trim=0:{h},setpts=PTS-STARTPTS[hv];"
        f"[0:a]atrim=0:{h},asetpts=PTS-STARTPTS[ha];"
        f"[0:v]trim={h},setpts=(PTS-STARTPTS)/1.05,noise=alls=5:allf=t[bv];"
        f"[0:a]atrim={h},asetpts=PTS-STARTPTS,atempo=1.05[ba];"
        f"[hv][ha][bv][ba]concat=n=2:v=1:a=1[v][a]"
    )


def polish(src: Path, dst: Path, *, hook_seconds: float) -> Path:
    _run([FFMPEG, "-y", "-v", "error", "-i", str(src),
          "-filter_complex", build_polish_filter(hook_seconds=hook_seconds),
          "-map", "[v]", "-map", "[a]",
          "-c:v", "libx264", "-preset", "fast", "-crf", "20",
          "-c:a", "aac", "-ar", "44100", "-ac", "2",
          "-movflags", "+faststart", str(dst)])
    return dst


def detect_silences_cmd(audio: Path, *, noise: str, min_d: float) -> list[str]:
    """The silencedetect invocation; caller captures stderr and feeds it
    to captions.parse_silencedetect."""
    return [FFMPEG, "-hide_banner", "-i", str(audio),
            "-af", f"silencedetect=noise={noise}:d={min_d}",
            "-f", "null", "-"]


def detect_silences(audio: Path, *, noise: str, min_d: float) -> str:
    r = subprocess.run(
        detect_silences_cmd(audio, noise=noise, min_d=min_d),
        capture_output=True, text=True,
    )
    return r.stderr
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_studio_captions.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/strategy_single_take/assemble.py tests/test_studio_captions.py
git commit -m "feat(studio): ffmpeg assemble — normalize/concat/burn/polish"
```

---

### Task 7: judge.py — Gemini video QC with fallback rotation

Port of `/opt/tg-bot/tools/reel_judge.py` (upload + rubric) as a library: no sys.exit, model rotation `gemini-2.5-flash → gemini-2.5-flash-lite` on 429/503 (flash free tier 429s all day; flash-lite gets through — validated 2026-07-06).

**Files:**
- Create: `app/services/strategy_single_take/judge.py`
- Test: `tests/test_studio_judge.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_studio_judge.py`:

```python
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
        if judge_mod.JUDGE_MODELS[0] in url:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_studio_judge.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Create `app/services/strategy_single_take/judge.py`:

```python
"""Gemini video QC — port of /opt/tg-bot/tools/reel_judge.py.

Non-blocking by contract: the worker catches JudgeError and still marks
the job READY (badge «QC недоступен»). Free-tier flash 429s routinely;
flash-lite is the fallback that actually answers.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests


BASE = "https://generativelanguage.googleapis.com"
JUDGE_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
RETRYABLE = {429, 500, 503}

RUBRIC_PROMPT = """\
Ты — строгий судья качества коротких вертикальных видео (Instagram Reels) для AI-UGC пайплайна.
Оцени видео по рубрике. Не льсти: 5 — это «средний живой UGC», 8+ — только если реально сильно.

Верни СТРОГО JSON:
{
  "scores": {
    "hook": 0-10,
    "visual_quality": 0-10,
    "text_readability": 0-10,
    "lipsync": 0-10,
    "audio": 0-10,
    "pacing": 0-10,
    "authenticity": 0-10
  },
  "overall": 0-10,
  "verdict": "pass" | "fix" | "reject",
  "top_issues": ["конкретная проблема с таймкодом"],
  "timeline_notes": ["0:00-0:02 ...", "..."]
}

Особое внимание: читаемость мелкого текста на этикетках продукта,
переходы между склейками, синхрон губ. Все замечания — с таймкодами.
"""


class JudgeError(RuntimeError):
    pass


def upload_video(path: Path, key: str) -> str:
    """Resumable upload to Gemini Files API → file_uri (waits for ACTIVE)."""
    size = path.stat().st_size
    start = requests.post(
        f"{BASE}/upload/v1beta/files",
        params={"key": key},
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": "video/mp4",
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": path.name}},
        timeout=30,
    )
    start.raise_for_status()
    upload_url = start.headers["X-Goog-Upload-URL"]

    with open(path, "rb") as f:
        up = requests.post(
            upload_url,
            headers={
                "X-Goog-Upload-Command": "upload, finalize",
                "X-Goog-Upload-Offset": "0",
                "Content-Length": str(size),
            },
            data=f,
            timeout=300,
        )
    up.raise_for_status()
    info = up.json()["file"]

    name = info["name"]
    for _ in range(60):
        if info.get("state") == "ACTIVE":
            return info["uri"]
        if info.get("state") == "FAILED":
            raise JudgeError(f"Gemini не смог обработать видео: {info}")
        time.sleep(2)
        info = requests.get(
            f"{BASE}/v1beta/{name}", params={"key": key}, timeout=30,
        ).json()
    raise JudgeError("Таймаут: видео не стало ACTIVE за 2 минуты")


def judge_uploaded(file_uri: str, *, key: str, brief: str | None) -> dict:
    """Rotate through JUDGE_MODELS on quota/transient codes."""
    prompt = RUBRIC_PROMPT
    if brief:
        prompt += f"\nКонтекст от автора (что задумывалось): {brief}\n"
    body = {
        "contents": [{
            "parts": [
                {"file_data": {"file_uri": file_uri, "mime_type": "video/mp4"}},
                {"text": prompt},
            ],
        }],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.2,
        },
    }
    last = ""
    for model in JUDGE_MODELS:
        r = requests.post(
            f"{BASE}/v1beta/models/{model}:generateContent",
            params={"key": key}, json=body, timeout=300,
        )
        if r.status_code == 200:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        last = f"{model}: HTTP {r.status_code}"
        if r.status_code not in RETRYABLE:
            raise JudgeError(f"judge hard fail — {last}: {r.text[:300]}")
    raise JudgeError(f"все judge-модели исчерпаны — {last}")


def judge_video(path: Path, *, api_key: str, brief: str | None = None) -> dict:
    uri = upload_video(path, api_key)
    return judge_uploaded(uri, key=api_key, brief=brief)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_studio_judge.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/strategy_single_take/judge.py tests/test_studio_judge.py
git commit -m "feat(studio): Gemini video judge with flash→flash-lite rotation"
```

### Task 8: studio_service.py — job creation

Mirrors `makeugc_service.create_makeugc_job_async`; reuses its validation constants (imported, not copied).

**Files:**
- Create: `app/services/studio_service.py`
- Test: `tests/test_studio_service.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_studio_service.py`:

```python
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
    from app.models.studio_job import StudioStatus

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_studio_service.py -v`
Expected: new tests FAIL with `ModuleNotFoundError: app.services.studio_service`

- [ ] **Step 3: Implement**

Create `app/services/studio_service.py`:

```python
"""Studio job create orchestration — validate, upload inputs to R2,
insert a PENDING row; studio_worker drains it."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.storage import get_r2
from app.models.studio_job import StudioJob, StudioStatus
from app.models.user import User
from app.services.makeugc_service import (
    ALLOWED_BROLL_CONTENT_TYPES,
    ALLOWED_PRODUCT_CONTENT_TYPES,
    MAX_BROLL_BYTES,
    MAX_PRODUCT_IMAGE_BYTES,
    MAX_PRODUCT_IMAGES,
)


VOICE_STYLES = {"normal", "asmr"}


class StudioValidationError(ValueError):
    pass


def create_studio_job_async(
    db: Session,
    user: User,
    *,
    product_images: list[tuple[bytes, str]],
    product_name: str,
    brand: str,
    price_rub: Decimal,
    dupe_price_rub: Decimal,
    script_text: str | None,
    voice_style: str,
    captions_enabled: bool,
    hook_video: tuple[bytes, str] | None = None,
) -> StudioJob:
    product_name = (product_name or "").strip()
    brand = (brand or "").strip()
    if not product_name:
        raise StudioValidationError("product_name required")
    if not brand:
        raise StudioValidationError("brand required")
    if voice_style not in VOICE_STYLES:
        raise StudioValidationError(
            f"unknown voice_style: {voice_style} (allowed: {sorted(VOICE_STYLES)})"
        )
    if price_rub <= 0 or dupe_price_rub <= 0:
        raise StudioValidationError("prices must be > 0")
    if not product_images:
        raise StudioValidationError("at least one product image required")
    if len(product_images) > MAX_PRODUCT_IMAGES:
        raise StudioValidationError(
            f"too many product images (max {MAX_PRODUCT_IMAGES})"
        )

    image_specs: list[tuple[bytes, str, str]] = []
    for idx, (blob, ct) in enumerate(product_images):
        if not blob:
            raise StudioValidationError(f"image #{idx + 1} is empty")
        if len(blob) > MAX_PRODUCT_IMAGE_BYTES:
            raise StudioValidationError(
                f"image #{idx + 1} too large "
                f"(max {MAX_PRODUCT_IMAGE_BYTES // (1024 * 1024)} MB)"
            )
        ext = ALLOWED_PRODUCT_CONTENT_TYPES.get(ct)
        if not ext:
            raise StudioValidationError(f"image #{idx + 1}: unsupported type {ct}")
        if ext in ("heic", "heif", "avif"):
            try:
                import io
                from app.services.strategy_makeugc.collage import _open_image
                img = _open_image(blob)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=92)
                blob, ext, ct = buf.getvalue(), "jpg", "image/jpeg"
            except Exception as e:
                raise StudioValidationError(
                    f"image #{idx + 1}: cannot decode HEIC/AVIF — {e}"
                )
        image_specs.append((blob, ext, ct))

    hook_spec: tuple[bytes, str, str] | None = None
    if hook_video:
        blob, ct = hook_video
        if not blob:
            raise StudioValidationError("hook video is empty")
        if len(blob) > MAX_BROLL_BYTES:
            raise StudioValidationError(
                f"hook video too large (max {MAX_BROLL_BYTES // (1024 * 1024)} MB)"
            )
        ext = ALLOWED_BROLL_CONTENT_TYPES.get(ct)
        if not ext:
            raise StudioValidationError(f"unsupported hook video type: {ct}")
        hook_spec = (blob, ext, ct)

    r2 = get_r2()
    key_uuid = uuid.uuid4().hex[:12]
    keys: list[str] = []
    for idx, (blob, ext, ct) in enumerate(image_specs):
        key = f"users/{user.id}/studio/{key_uuid}/product-{idx + 1}.{ext}"
        r2.upload_bytes(key, blob, content_type=ct)
        keys.append(key)

    hook_key: str | None = None
    if hook_spec:
        blob, ext, ct = hook_spec
        hook_key = f"users/{user.id}/studio/{key_uuid}/hook.{ext}"
        r2.upload_bytes(hook_key, blob, content_type=ct)

    job = StudioJob(
        user_id=user.id,
        product_image_keys=keys,
        product_name=product_name,
        brand=brand,
        price_rub=price_rub,
        dupe_price_rub=dupe_price_rub,
        script_text=(script_text or "").strip() or None,
        voice_style=voice_style,
        captions_enabled=captions_enabled,
        hook_video_key=hook_key,
        status=StudioStatus.PENDING,
        cost_usd=Decimal("0"),
        created_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_studio_service.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/studio_service.py tests/test_studio_service.py
git commit -m "feat(studio): job create service"
```

### Task 9: studio_worker.py — pipeline orchestration

Same polling-thread pattern as `makeugc_worker.py` (SELECT FOR UPDATE SKIP LOCKED, resume-safe stage skips, per-stage cost accrual). Key resolution POC rules: Replicate = `user.replicate_api_key` or env `REPLICATE_API_TOKEN`; ElevenLabs = same rule as makeugc (`resolve_api_key`/`resolve_voice_id`); judge = env `GEMINI_API_KEY`, non-blocking.

**Files:**
- Create: `app/workers/studio_worker.py`
- Test: `tests/test_studio_service.py` (append)

- [ ] **Step 1: Write the failing orchestration test**

Append to `tests/test_studio_service.py`:

```python
def _make_pending_job(db_session, test_user, fake_r2_worker, **over):
    from app.models.studio_job import StudioJob, StudioStatus
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
    from app.models.studio_job import StudioStatus

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
    from app.models.studio_job import StudioStatus
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
    from app.models.studio_job import StudioStatus

    def boom(**kw):
        raise RuntimeError("nano-banana упал")
    monkeypatch.setattr(w, "generate_studio_portrait", boom)
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r-key")

    j = _make_pending_job(db_session, test_user, fake_r2_worker)
    w.process_job(db_session, j, test_user)

    assert j.status == StudioStatus.FAILED
    assert "portrait" in j.error_message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_studio_service.py -v`
Expected: 3 new tests FAIL with `ModuleNotFoundError: app.workers.studio_worker`

- [ ] **Step 3: Implement**

Create `app/workers/studio_worker.py`:

```python
"""Drains StudioJob rows: PENDING → PORTRAIT → VOICEOVER → LIPSYNC →
ASSEMBLE → JUDGE → READY / FAILED. Same polling pattern as
makeugc_worker; one job at a time; stage keys + cost_usd written as it
goes so a retry after FAILED skips completed stages."""
from __future__ import annotations

import logging
import os
import tempfile
import time
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.storage import get_r2
from app.models.studio_job import StudioJob, StudioStatus
from app.models.user import User
from app.services.media_helpers import download_bytes
from app.services.replicate_client import (
    ReplicateError,
    ReplicateSafetyError,
    ReplicateTransientError,
)
from app.services.strategy_makeugc.lipsync import generate_lipsync
from app.services.strategy_makeugc.voiceover import (
    VoiceoverError,
    resolve_api_key as resolve_eleven_key,
    resolve_voice_id,
)
from app.services.strategy_single_take.assemble import (
    AssembleError,
    burn_captions,
    concat_clips,
    detect_silences,
    normalize_clip,
    polish,
    probe_duration,
)
from app.services.strategy_single_take.captions import (
    SILENCE_ASMR,
    SILENCE_NORMAL,
    align_sentences,
    build_ass,
    parse_silencedetect,
    speech_spans,
    split_sentences,
)
from app.services.strategy_single_take.judge import JudgeError, judge_video
from app.services.strategy_single_take.portrait import generate_studio_portrait
from app.services.strategy_single_take.voiceover import generate_voiceover_v3


ELEVENLABS_USD_PER_1K_CHARS = 0.30

log = logging.getLogger(__name__)


def pick_next_pending(db: Session) -> StudioJob | None:
    return (
        db.query(StudioJob)
        .filter(StudioJob.status == StudioStatus.PENDING)
        .order_by(StudioJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )


def _fail(db: Session, j: StudioJob, msg: str) -> None:
    j.status = StudioStatus.FAILED
    j.error_message = msg[:500]
    j.completed_at = datetime.utcnow()
    db.commit()


def _mark(db: Session, j: StudioJob, stage: StudioStatus) -> None:
    j.status = stage
    db.commit()


def _add_cost(db: Session, j: StudioJob, usd: float) -> None:
    j.cost_usd = (j.cost_usd or Decimal("0")) + Decimal(str(round(usd, 4)))
    db.commit()


def _get_blob(r2, key: str) -> bytes:
    obj = r2._client.get_object(Bucket=r2.bucket, Key=key)
    return obj["Body"].read()


def _to_bytes(result, timeout: int) -> bytes:
    if isinstance(result, (bytes, bytearray)):
        return bytes(result)
    return download_bytes(result, timeout=timeout)


def _assemble(
    j: StudioJob,
    tmp: Path,
    lipsync_path: Path,
    voiceover_path: Path,
    hook_path: Path | None,
) -> Path:
    """normalize → optional hook concat → captions → polish. Returns final mp4."""
    body = normalize_clip(lipsync_path, tmp / "body.mp4")
    hook_seconds = 0.0
    if hook_path is not None:
        hook_n = normalize_clip(hook_path, tmp / "hook_n.mp4")
        hook_seconds = probe_duration(hook_n)
        raw = concat_clips([hook_n, body], tmp / "raw.mp4")
    else:
        raw = body

    staged = raw
    if j.captions_enabled and j.script_text:
        noise, min_d = SILENCE_ASMR if j.voice_style == "asmr" else SILENCE_NORMAL
        stderr = detect_silences(voiceover_path, noise=noise, min_d=min_d)
        vo_total = probe_duration(voiceover_path)
        spans = speech_spans(parse_silencedetect(stderr), total=vo_total)
        sents = split_sentences(j.script_text)
        aligned = align_sentences(sents, spans)
        # shift into final timeline (hook precedes the talking take)
        aligned = [(s + hook_seconds, e + hook_seconds, t) for s, e, t in aligned]
        ass_path = tmp / "captions.ass"
        ass_path.write_text(build_ass(aligned))
        staged = burn_captions(raw, ass_path, tmp / "subbed.mp4")

    return polish(staged, tmp / "final.mp4", hook_seconds=hook_seconds)


def process_job(db: Session, j: StudioJob, user: User) -> None:
    replicate_key = user.replicate_api_key or os.getenv("REPLICATE_API_TOKEN")
    if not replicate_key:
        _fail(db, j, "Нет Replicate ключа (user или env REPLICATE_API_TOKEN)")
        return

    r2 = get_r2()
    keys = list(j.product_image_keys or [])
    if not keys:
        _fail(db, j, "У job'а нет product image — удали и пересоздай")
        return
    product_bytes = _get_blob(r2, keys[0])

    # --- PORTRAIT (resume-safe) ---
    if not j.portrait_key:
        _mark(db, j, StudioStatus.PORTRAIT)
        try:
            result, cost = generate_studio_portrait(
                product_image_bytes=product_bytes,
                product_content_type="image/jpeg",
                product_name=j.product_name,
                brand=j.brand,
                asmr=(j.voice_style == "asmr"),
                replicate_api_key=replicate_key,
            )
            blob = _to_bytes(result, timeout=120)
        except ReplicateSafetyError as e:
            _fail(db, j, f"🛑 Moderation отбила portrait: {str(e)[:200]}")
            return
        except ReplicateTransientError as e:
            log.warning("studio %s transient on portrait: %s", j.id, e)
            raise
        except Exception as e:
            log.exception("studio %s portrait fail", j.id)
            _fail(db, j, f"ошибка portrait: {str(e)[:200]}")
            return
        key = f"users/{j.user_id}/studio/{j.id}/portrait-{uuid.uuid4().hex[:6]}.jpg"
        r2.upload_bytes(key, blob, content_type="image/jpeg")
        j.portrait_key = key
        _add_cost(db, j, cost)

    # --- VOICEOVER ---
    if not j.voiceover_key:
        _mark(db, j, StudioStatus.VOICEOVER)
        if not j.script_text:
            _fail(db, j, "Нет script_text — сгенерируй или введи текст")
            return
        eleven_key = resolve_eleven_key(user.elevenlabs_api_key)
        voice_id = resolve_voice_id(None)
        if not eleven_key or not voice_id:
            _fail(db, j, "Нет ELEVENLABS_API_KEY или MAKEUGC_DEFAULT_VOICE_ID")
            return
        try:
            audio = generate_voiceover_v3(
                script_text=j.script_text,
                voice_id=voice_id,
                api_key=eleven_key,
                asmr=(j.voice_style == "asmr"),
            )
        except VoiceoverError as e:
            _fail(db, j, f"TTS ошибка: {str(e)[:200]}")
            return
        key = f"users/{j.user_id}/studio/{j.id}/voiceover-{uuid.uuid4().hex[:6]}.mp3"
        r2.upload_bytes(key, audio, content_type="audio/mpeg")
        j.voiceover_key = key
        _add_cost(db, j, len(j.script_text) * ELEVENLABS_USD_PER_1K_CHARS / 1000.0)

    # --- LIPSYNC ---
    if not j.lipsync_key:
        _mark(db, j, StudioStatus.LIPSYNC)
        try:
            lip_result, lip_cost = generate_lipsync(
                portrait_bytes=_get_blob(r2, j.portrait_key),
                portrait_ext="jpg",
                voiceover_bytes=_get_blob(r2, j.voiceover_key),
                voiceover_ext="mp3",
                replicate_api_key=replicate_key,
            )
            video_blob = _to_bytes(lip_result, timeout=300)
        except ReplicateSafetyError as e:
            _fail(db, j, f"🛑 Moderation отбила lipsync: {str(e)[:200]}")
            return
        except ReplicateTransientError as e:
            log.warning("studio %s transient on lipsync: %s", j.id, e)
            raise
        except ReplicateError as e:
            _fail(db, j, f"Replicate fail on lipsync: {str(e)[:200]}")
            return
        except Exception as e:
            log.exception("studio %s lipsync fail", j.id)
            _fail(db, j, f"ошибка lipsync: {str(e)[:200]}")
            return
        key = f"users/{j.user_id}/studio/{j.id}/lipsync-{uuid.uuid4().hex[:6]}.mp4"
        r2.upload_bytes(key, video_blob, content_type="video/mp4")
        j.lipsync_key = key
        _add_cost(db, j, lip_cost)

    # --- ASSEMBLE + JUDGE (same tmpdir) ---
    _mark(db, j, StudioStatus.ASSEMBLE)
    with tempfile.TemporaryDirectory(prefix="studio_") as tmpdir:
        tmp = Path(tmpdir)
        lipsync_path = tmp / "lipsync.mp4"
        lipsync_path.write_bytes(_get_blob(r2, j.lipsync_key))
        voiceover_path = tmp / "voiceover.mp3"
        voiceover_path.write_bytes(_get_blob(r2, j.voiceover_key))
        hook_path: Path | None = None
        if j.hook_video_key:
            hook_path = tmp / "hook_src.mp4"
            hook_path.write_bytes(_get_blob(r2, j.hook_video_key))

        try:
            final_path = _assemble(j, tmp, lipsync_path, voiceover_path, hook_path)
        except AssembleError as e:
            _fail(db, j, f"ffmpeg pipeline failed: {str(e)[:300]}")
            return
        except Exception as e:
            log.exception("studio %s assemble fail", j.id)
            _fail(db, j, f"ошибка assemble: {str(e)[:200]}")
            return

        out_key = f"users/{j.user_id}/studio/{j.id}/final-{uuid.uuid4().hex[:6]}.mp4"
        r2.upload_bytes(out_key, final_path.read_bytes(), content_type="video/mp4")
        j.output_key = out_key
        db.commit()

        # --- JUDGE (non-blocking) ---
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key:
            _mark(db, j, StudioStatus.JUDGE)
            try:
                report = judge_video(
                    final_path, api_key=gemini_key,
                    brief=(
                        f"AI-UGC single-take рилс: аналог {j.brand} "
                        f"{j.product_name}, стиль {j.voice_style}"
                    ),
                )
                j.judge_report = report
                overall = report.get("overall")
                if isinstance(overall, (int, float)):
                    j.judge_score = int(round(overall))
                db.commit()
            except (JudgeError, Exception) as e:  # noqa: B014 — non-blocking by design
                log.warning("studio %s judge failed (non-blocking): %s", j.id, e)
                db.rollback()

    j.status = StudioStatus.READY
    j.completed_at = datetime.utcnow()
    db.commit()


def run_loop(db_factory, poll_seconds: float = 2.0) -> None:
    """Long-running drain loop. db_factory() returns a fresh Session per tick."""
    while True:
        db = db_factory()
        try:
            j = pick_next_pending(db)
            if j is None:
                db.commit()
                time.sleep(poll_seconds)
                continue
            try:
                user = db.query(User).get(j.user_id)
                if not user:
                    _fail(db, j, "user disappeared")
                    continue
                process_job(db, j, user)
            except ReplicateTransientError:
                db.rollback()
            except Exception as e:
                log.exception("studio %s hard fail", j.id)
                _fail(db, j, f"внутренняя ошибка: {str(e)[:200]}")
        finally:
            db.close()
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_studio_service.py -v`
Expected: all PASS. Watch for the TTS cost assertion: script "Я это заказала. Ну что?" = 24 chars → 0.0072 USD.

- [ ] **Step 5: Commit**

```bash
git add app/workers/studio_worker.py tests/test_studio_service.py
git commit -m "feat(studio): pipeline worker — portrait/voiceover/lipsync/assemble/judge"
```

### Task 10: api/studio.py — REST endpoints

**Files:**
- Create: `app/api/studio.py`
- Test: `tests/test_studio_service.py` (append — uses the `auth_client` fixture from conftest)

- [ ] **Step 1: Write the failing API tests**

Append to `tests/test_studio_service.py`:

```python
def test_api_create_and_list_and_retry(auth_client, db_session, test_user, fake_r2):
    from app.models.studio_job import StudioJob, StudioStatus

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
    from app.models.studio_job import StudioJob, StudioStatus
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
```

Note: these tests import the FastAPI app via `auth_client`, which requires the router to be mounted in `app/main.py` — Task 11 does the mount. Run these tests only after Task 11, or accept the FAIL here and proceed.

- [ ] **Step 2: Implement the router**

Create `app/api/studio.py`:

```python
"""REST API for the single-take UGC Studio (POC).

  GET  /api/studio/jobs/            — list current user's jobs
  POST /api/studio/jobs/            — multipart form → enqueue (202)
  GET  /api/studio/jobs/{jid}       — poll one job
  POST /api/studio/jobs/{jid}/retry — reset FAILED job to PENDING (full restart)
  POST /api/studio/script           — script autogen for the textarea
"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, UploadFile, status,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.studio_job import StudioJob, StudioStatus
from app.models.user import User
from app.services.strategy_makeugc.script import resolve_openai_key
from app.services.strategy_single_take.script import generate_studio_script
from app.services.studio_service import (
    StudioValidationError,
    create_studio_job_async,
)


router = APIRouter()


class StudioJobResponse(BaseModel):
    id: int
    product_name: str
    brand: str
    price_rub: float
    dupe_price_rub: float
    voice_style: str
    captions_enabled: bool
    script_text: Optional[str]
    hook_video_key: Optional[str]
    status: str
    portrait_key: Optional[str]
    voiceover_key: Optional[str]
    lipsync_key: Optional[str]
    output_key: Optional[str]
    judge_score: Optional[int]
    judge_report: Optional[dict]
    error_message: Optional[str]
    cost_usd: float
    created_at: str
    completed_at: Optional[str]

    @classmethod
    def from_model(cls, j: StudioJob) -> "StudioJobResponse":
        return cls(
            id=j.id,
            product_name=j.product_name,
            brand=j.brand,
            price_rub=float(j.price_rub),
            dupe_price_rub=float(j.dupe_price_rub),
            voice_style=j.voice_style,
            captions_enabled=bool(j.captions_enabled),
            script_text=j.script_text,
            hook_video_key=j.hook_video_key,
            status=j.status,
            portrait_key=j.portrait_key,
            voiceover_key=j.voiceover_key,
            lipsync_key=j.lipsync_key,
            output_key=j.output_key,
            judge_score=j.judge_score,
            judge_report=j.judge_report,
            error_message=j.error_message,
            cost_usd=float(j.cost_usd or 0),
            created_at=j.created_at.isoformat(),
            completed_at=j.completed_at.isoformat() if j.completed_at else None,
        )


class StudioJobListResponse(BaseModel):
    items: list[StudioJobResponse]


class ScriptRequest(BaseModel):
    product_name: str
    brand: str
    price_rub: float
    dupe_price_rub: float
    voice_style: str = "normal"


def _get_owned(db: Session, user: User, jid: int) -> StudioJob:
    j = (
        db.query(StudioJob)
        .filter(StudioJob.id == jid, StudioJob.user_id == user.id)
        .first()
    )
    if not j:
        raise HTTPException(404, "studio job not found")
    return j


@router.get("/jobs/", response_model=StudioJobListResponse)
def list_jobs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(StudioJob)
        .filter(StudioJob.user_id == current_user.id)
        .order_by(StudioJob.created_at.desc())
        .all()
    )
    return StudioJobListResponse(
        items=[StudioJobResponse.from_model(r) for r in rows]
    )


@router.post(
    "/jobs/",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StudioJobResponse,
)
async def create_job(
    product_images: list[UploadFile] = File(...),
    product_name: str = Form(...),
    brand: str = Form(...),
    price_rub: str = Form(...),
    dupe_price_rub: str = Form(...),
    script_text: Optional[str] = Form(None),
    voice_style: str = Form("normal"),
    captions_enabled: bool = Form(True),
    hook_video: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        price_dec = Decimal(price_rub)
        dupe_dec = Decimal(dupe_price_rub)
    except InvalidOperation:
        raise HTTPException(400, "price_rub and dupe_price_rub must be decimals")

    images: list[tuple[bytes, str]] = []
    for f in product_images:
        blob = await f.read()
        images.append((blob, f.content_type or "application/octet-stream"))

    hook_pair: tuple[bytes, str] | None = None
    if hook_video is not None and hook_video.filename:
        hook_blob = await hook_video.read()
        if hook_blob:
            hook_pair = (
                hook_blob, hook_video.content_type or "application/octet-stream",
            )

    try:
        j = create_studio_job_async(
            db, current_user,
            product_images=images,
            product_name=product_name,
            brand=brand,
            price_rub=price_dec,
            dupe_price_rub=dupe_dec,
            script_text=script_text,
            voice_style=voice_style,
            captions_enabled=captions_enabled,
            hook_video=hook_pair,
        )
    except StudioValidationError as e:
        raise HTTPException(400, str(e))
    return StudioJobResponse.from_model(j)


@router.get("/jobs/{jid}", response_model=StudioJobResponse)
def get_job(
    jid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return StudioJobResponse.from_model(_get_owned(db, current_user, jid))


@router.post("/jobs/{jid}/retry", response_model=StudioJobResponse)
def retry_job(
    jid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    j = _get_owned(db, current_user, jid)
    if j.status not in (StudioStatus.FAILED, StudioStatus.READY):
        raise HTTPException(409, f"job is {j.status}, can't retry")
    # POC: full pipeline restart — clear all stage artifacts.
    # (resume-from-stage is a spec'd follow-up; cost_usd keeps accruing
    # because the money was actually spent)
    j.status = StudioStatus.PENDING
    j.error_message = None
    j.portrait_key = None
    j.voiceover_key = None
    j.lipsync_key = None
    j.output_key = None
    j.judge_score = None
    j.judge_report = None
    j.completed_at = None
    db.commit()
    db.refresh(j)
    return StudioJobResponse.from_model(j)


@router.post("/script")
def make_script(
    req: ScriptRequest,
    current_user: User = Depends(get_current_user),
):
    openai_key = resolve_openai_key(current_user.openai_api_key)
    if not openai_key:
        raise HTTPException(400, "Нет OPENAI_API_KEY (ни у юзера, ни в env)")
    try:
        text = generate_studio_script(
            product_name=req.product_name,
            brand=req.brand,
            price_rub=req.price_rub,
            dupe_price_rub=req.dupe_price_rub,
            voice_style=req.voice_style,
            openai_api_key=openai_key,
        )
    except Exception as e:
        raise HTTPException(502, f"script-gen ошибка: {str(e)[:200]}")
    return {"script_text": text}
```

- [ ] **Step 3: Commit (tests still red until Task 11 mounts the router)**

```bash
git add app/api/studio.py tests/test_studio_service.py
git commit -m "feat(studio): REST API — jobs CRUD, retry, script autogen"
```

---

### Task 11: main.py wiring — router, /studio route, worker thread

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Mount the router**

In the router-import block (after line `from app.api.makeugc import router as makeugc_router`, ~line 558) add:

```python
from app.api.studio import router as studio_router
```

After `app.include_router(makeugc_router, ...)` (~line 579) add:

```python
app.include_router(studio_router, prefix="/api/studio", tags=["Studio"])
```

- [ ] **Step 2: Add the page route**

Next to the `/makeugc` route (~line 612) add:

```python
@app.get("/studio")
async def studio_page():
    return FileResponse("static/studio.html")
```

- [ ] **Step 3: Start the worker thread in lifespan**

After the MakeUGC worker block (~line 490) add:

```python
    # Studio worker — drains studio_jobs rows through the single-take
    # pipeline (portrait → voiceover → lipsync → assemble → judge).
    if os.getenv("WORKER_STUDIO", "1") == "1":
        from app.database import SessionLocal as _StudioSession
        from app.workers.studio_worker import run_loop as studio_loop
        threading.Thread(
            target=studio_loop, args=(_StudioSession,),
            daemon=True, name="studio-worker",
        ).start()
        logger.info("✅ Studio worker запущен")
```

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m pytest tests/ -v`
Expected: all tests PASS, including Task 10's API tests (router now mounted).

- [ ] **Step 5: Smoke-check app import**

Run: `python3 -c "from app.main import app; print('ok', len(app.routes))"`
Expected: prints `ok <N>` with no import errors.

- [ ] **Step 6: Commit**

```bash
git add app/main.py
git commit -m "feat(studio): mount /api/studio + /studio page + worker thread"
```

### Task 12: static/studio.html — UI

Glassmorphism page consistent with the rest of the SPA (Tailwind CDN, dark gradient, `backdrop-blur` cards). Left column — form; right column — job cards with stage chips, polling every 5s. Auth helpers are copied verbatim from `static/makeugc.html` lines 199–238 (`ACCESS`/`hdr()`/`refresh()`/logout) — same token-refresh contract.

**Files:**
- Create: `static/studio.html`

- [ ] **Step 1: Create the page**

Create `static/studio.html`. Structure (full file; auth block marked below is the verbatim copy from `makeugc.html:199-238`):

```html
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Studio — ReelsTracker</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body { background: linear-gradient(135deg,#0f0c29,#302b63,#24243e); min-height:100vh; }
  .glass { background: rgba(255,255,255,.07); backdrop-filter: blur(14px);
           border:1px solid rgba(255,255,255,.12); border-radius:1rem; }
  .chip { font-size:.65rem; padding:.15rem .5rem; border-radius:9999px;
          border:1px solid rgba(255,255,255,.2); }
  .chip-done { background:#16a34a33; border-color:#16a34a; color:#86efac; }
  .chip-active { background:#eab30833; border-color:#eab308; color:#fde047;
                 animation: pulse 1.2s infinite; }
  .chip-fail { background:#dc262633; border-color:#dc2626; color:#fca5a5; }
</style>
</head>
<body class="text-white">
<nav class="glass m-4 px-6 py-3 flex items-center gap-6 text-sm">
  <a href="/" class="font-bold">ReelsTracker</a>
  <a href="/forge" class="opacity-70 hover:opacity-100">Forge</a>
  <a href="/makeugc" class="opacity-70 hover:opacity-100">MakeUGC</a>
  <span class="font-semibold text-amber-300">Studio</span>
  <button onclick="logout()" class="ml-auto opacity-60 hover:opacity-100">выйти</button>
</nav>

<div class="max-w-6xl mx-auto p-4 grid md:grid-cols-[380px_1fr] gap-6">
  <!-- ФОРМА -->
  <div class="glass p-6 space-y-4 self-start">
    <h1 class="text-xl font-bold">Single-take рилс</h1>
    <div>
      <label class="text-xs opacity-70">Название продукта (как на этикетке)</label>
      <input id="f-name" class="w-full mt-1 bg-white/10 rounded-lg px-3 py-2 text-sm" placeholder="WHITE CHOCOLATE">
    </div>
    <div>
      <label class="text-xs opacity-70">Бренд-оригинал</label>
      <input id="f-brand" class="w-full mt-1 bg-white/10 rounded-lg px-3 py-2 text-sm" placeholder="Richard Maison de Parfum">
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="text-xs opacity-70">Цена оригинала ₽</label>
        <input id="f-dupe-price" type="number" class="w-full mt-1 bg-white/10 rounded-lg px-3 py-2 text-sm" placeholder="16000">
      </div>
      <div>
        <label class="text-xs opacity-70">Наша цена ₽</label>
        <input id="f-price" type="number" class="w-full mt-1 bg-white/10 rounded-lg px-3 py-2 text-sm" placeholder="1990">
      </div>
    </div>
    <div>
      <label class="text-xs opacity-70">Фото продукта (1-4)</label>
      <input id="f-photos" type="file" accept="image/*" multiple
             class="w-full mt-1 text-xs file:bg-white/10 file:border-0 file:rounded-lg file:px-3 file:py-2 file:text-white file:mr-3">
    </div>
    <div>
      <div class="flex items-center justify-between">
        <label class="text-xs opacity-70">Сценарий</label>
        <button id="btn-script" onclick="genScript()" class="text-xs text-amber-300 hover:underline">сгенерировать</button>
      </div>
      <textarea id="f-script" rows="5" class="w-full mt-1 bg-white/10 rounded-lg px-3 py-2 text-sm"
        placeholder="Оставь пустым и нажми «сгенерировать», или впиши свой текст"></textarea>
    </div>
    <div class="flex items-center gap-4">
      <label class="text-xs opacity-70">Голос:</label>
      <label class="text-sm"><input type="radio" name="voice" value="normal" checked> обычный</label>
      <label class="text-sm"><input type="radio" name="voice" value="asmr"> ASMR-шёпот</label>
    </div>
    <label class="text-sm flex items-center gap-2">
      <input id="f-captions" type="checkbox" checked> субтитры (word-by-word)
    </label>
    <div>
      <label class="text-xs opacity-70">Hook-клип (опционально, mp4/mov)</label>
      <input id="f-hook" type="file" accept="video/*"
             class="w-full mt-1 text-xs file:bg-white/10 file:border-0 file:rounded-lg file:px-3 file:py-2 file:text-white file:mr-3">
    </div>
    <button id="btn-go" onclick="createJob()"
      class="w-full bg-amber-400 text-black font-bold rounded-xl py-3 hover:bg-amber-300 disabled:opacity-40">
      Сделать рилс
    </button>
    <p id="form-err" class="text-xs text-red-300"></p>
  </div>

  <!-- ДЖОБЫ -->
  <div id="jobs" class="space-y-4"></div>
</div>

<script>
/* ── auth: скопировано 1:1 из makeugc.html (строки 199-238) ── */
let ACCESS = localStorage.getItem('access_token');
if (!ACCESS) location.href = '/login.html';
function hdr() { return { 'Authorization': 'Bearer ' + ACCESS }; }
/* …вставь сюда остальной auth-блок из makeugc.html: apiFetch()-обёртку
   с refresh_token-ретраем на 401 и logout() — БЕЗ ИЗМЕНЕНИЙ… */

/* ── Studio ── */
const STAGES = [
  ['portrait',  'Портрет'],
  ['voiceover', 'Озвучка'],
  ['lipsync',   'Липсинк'],
  ['assemble',  'Сборка'],
  ['judge',     'QC'],
];
const ORDER = ['pending','portrait','voiceover','lipsync','assemble','judge','ready'];

function chips(j) {
  const idx = ORDER.indexOf(j.status);
  return STAGES.map(([key, label]) => {
    const kidx = ORDER.indexOf(key);
    let cls = 'chip opacity-50';
    if (j.status === 'failed') {
      cls = kidx < idx || (j[key + '_key'] || (key === 'judge' && j.judge_score != null))
        ? 'chip chip-done' : 'chip chip-fail';
    } else if (j.status === 'ready' || kidx < idx) cls = 'chip chip-done';
    else if (kidx === idx) cls = 'chip chip-active';
    return `<span class="${cls}">${label}</span>`;
  }).join(' <span class="opacity-30">→</span> ');
}

function judgeBadge(j) {
  if (j.status !== 'ready') return '';
  if (j.judge_score == null)
    return '<span class="chip opacity-60">QC недоступен</span>';
  const v = (j.judge_report && j.judge_report.verdict) || '';
  const color = j.judge_score >= 8 ? 'chip-done' : (j.judge_score >= 6 ? 'chip-active' : 'chip-fail');
  return `<span class="chip ${color}">QC ${j.judge_score}/10 ${v}</span>`;
}

function card(j) {
  const media = j.status === 'ready' && j.output_key
    ? `<video controls playsinline class="rounded-xl w-full max-w-[240px] aspect-[9/16] object-cover bg-black"
         src="/api/media?key=${encodeURIComponent(j.output_key)}"></video>`
    : (j.portrait_key
        ? `<img src="/api/media?key=${encodeURIComponent(j.portrait_key)}"
             class="rounded-xl w-full max-w-[240px] aspect-[9/16] object-cover">`
        : '<div class="rounded-xl w-full max-w-[240px] aspect-[9/16] bg-white/5 flex items-center justify-center text-4xl">🎬</div>');
  const err = j.error_message
    ? `<p class="text-xs text-red-300 mt-2">${j.error_message}</p>` : '';
  const retry = (j.status === 'failed' || j.status === 'ready')
    ? `<button onclick="retryJob(${j.id})" class="text-xs text-amber-300 hover:underline mt-2">перегенерить</button>` : '';
  return `<div class="glass p-4 flex gap-4">
    <div class="shrink-0 w-[160px] md:w-[200px]">${media}</div>
    <div class="min-w-0 flex-1">
      <div class="flex items-center gap-2 flex-wrap">
        <span class="font-semibold">${j.product_name}</span>
        <span class="text-xs opacity-60">${j.brand}</span>
        <span class="text-xs opacity-60">$${j.cost_usd.toFixed(2)}</span>
        ${judgeBadge(j)}
      </div>
      <div class="mt-2 flex items-center gap-1 flex-wrap">${chips(j)}</div>
      ${err}${retry}
    </div>
  </div>`;
}

async function loadJobs() {
  const r = await apiFetch('/api/studio/jobs/');
  if (!r.ok) return;
  const d = await r.json();
  document.getElementById('jobs').innerHTML =
    d.items.map(card).join('') ||
    '<div class="glass p-8 text-center opacity-60">Пока нет рилсов — заполни форму слева</div>';
}

async function genScript() {
  const btn = document.getElementById('btn-script');
  btn.textContent = 'генерирую…';
  try {
    const r = await apiFetch('/api/studio/script', {
      method: 'POST',
      headers: { ...hdr(), 'Content-Type': 'application/json' },
      body: JSON.stringify({
        product_name: document.getElementById('f-name').value,
        brand: document.getElementById('f-brand').value,
        price_rub: +document.getElementById('f-price').value || 0,
        dupe_price_rub: +document.getElementById('f-dupe-price').value || 0,
        voice_style: document.querySelector('input[name=voice]:checked').value,
      }),
    });
    const d = await r.json();
    if (r.ok) document.getElementById('f-script').value = d.script_text;
    else document.getElementById('form-err').textContent = d.detail || 'ошибка';
  } finally { btn.textContent = 'сгенерировать'; }
}

async function createJob() {
  const err = document.getElementById('form-err');
  err.textContent = '';
  const photos = document.getElementById('f-photos').files;
  if (!photos.length) { err.textContent = 'Добавь хотя бы одно фото продукта'; return; }
  const fd = new FormData();
  for (const f of photos) fd.append('product_images', f);
  fd.append('product_name', document.getElementById('f-name').value);
  fd.append('brand', document.getElementById('f-brand').value);
  fd.append('price_rub', document.getElementById('f-price').value);
  fd.append('dupe_price_rub', document.getElementById('f-dupe-price').value);
  fd.append('script_text', document.getElementById('f-script').value);
  fd.append('voice_style', document.querySelector('input[name=voice]:checked').value);
  fd.append('captions_enabled', document.getElementById('f-captions').checked);
  const hook = document.getElementById('f-hook').files[0];
  if (hook) fd.append('hook_video', hook);

  const btn = document.getElementById('btn-go');
  btn.disabled = true; btn.textContent = 'Отправляю…';
  try {
    const r = await apiFetch('/api/studio/jobs/', { method: 'POST', headers: hdr(), body: fd });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.detail || 'ошибка'; return; }
    loadJobs();
  } finally { btn.disabled = false; btn.textContent = 'Сделать рилс'; }
}

async function retryJob(id) {
  await apiFetch(`/api/studio/jobs/${id}/retry`, { method: 'POST', headers: hdr() });
  loadJobs();
}

loadJobs();
setInterval(loadJobs, 5000);
</script>
</body>
</html>
```

Implementation note for the executor: the `/* …вставь сюда… */` marker is NOT a placeholder to skip — open `static/makeugc.html`, copy its lines 199–238 (the `apiFetch` wrapper with 401→refresh-token retry, plus `logout()`) and paste them there unchanged. If makeugc.html's helper is named differently (e.g. plain `fetch` + `refreshToken()`), adopt its exact names and adjust the call-sites in this file (`apiFetch(` → the real name) so both pages share one convention.

- [ ] **Step 2: Manual smoke test in browser**

```bash
cd /opt/projects/reelstracker-saas && python3 run.py
```

Open `http://localhost:8000/studio` (login first at `/login.html`). Verify: form renders, «сгенерировать» fills the textarea (needs `OPENAI_API_KEY`), submitting without photos shows the inline error, submitting with a photo creates a card with a pulsing «Портрет» chip. Stop the server after.

- [ ] **Step 3: Commit**

```bash
git add static/studio.html
git commit -m "feat(studio): glassmorphism UI page"
```

---

### Task 13: Full suite, push, PR

- [ ] **Step 1: Run everything**

```bash
cd /opt/projects/reelstracker-saas && python3 -m pytest tests/ -v
```

Expected: full suite green (including pre-existing tests — regressions here mean an import in `app/main.py` or `app/models/__init__.py` broke something).

- [ ] **Step 2: Self-review against the spec**

Re-read `docs/specs/2026-07-06-studio-single-take-design.md` §Architecture and §Out of scope. Confirm: no credit debiting code, no autoposting, no gesture inserts, no per-user Gemini keys, no handheld shake in polish.

- [ ] **Step 3: Push + PR**

```bash
git push -u origin feat/studio-single-take
gh pr create --title "Studio: single-take UGC reel builder (POC)" --body "$(cat <<'EOF'
## Summary
- New /studio page: form → PORTRAIT → VOICEOVER → LIPSYNC → ASSEMBLE → JUDGE → READY pipeline
- Ports the vault wc_single_take v36 recipe: nano-banana-pro portrait with label ban-list, eleven_v3 ASMR whispers, Pruna lipsync (reused), word-by-word ASS captions from silencedetect, ×1.05 polish + grain, Gemini video QC with flash→flash-lite fallback (non-blocking)
- Spec: docs/specs/2026-07-06-studio-single-take-design.md

## Test plan
- [ ] `pytest tests/` green (captions math, silencedetect parser, judge rotation, service+worker orchestration, API)
- [ ] Manual: full run against real APIs with a dose product photo, verify reel in browser
EOF
)"
```

Expected: PR URL printed. Report it to Nick in TG with a one-line summary + what env keys Railway needs (`GEMINI_API_KEY` new; `REPLICATE_API_TOKEN` as server-side fallback).

---

## Self-review notes (done at plan-writing time)

- Spec coverage: model ✅ (T1), captions ✅ (T2), voiceover ✅ (T3), portrait ✅ (T4), script autogen ✅ (T5), assemble+polish ✅ (T6), judge ✅ (T7), service ✅ (T8), worker ✅ (T9), API incl. retry+script ✅ (T10), main.py routes+worker ✅ (T11), UI ✅ (T12). Out-of-scope items intentionally absent.
- Judge non-blocking: worker catches all judge exceptions → READY (test in T9).
- Retry-restarts-whole-pipeline: T10 clears all stage keys (spec: resume is a follow-up).
- Type consistency: `generate_studio_portrait` returns `(bytes|str, float)` matching makeugc stage convention; `_assemble(job, tmp, lipsync_path, voiceover_path, hook_path)` signature identical in worker and in T9 test mocks.
- ffmpeg binary: bare `ffmpeg`/`ffprobe` names match `strategy_makeugc/concat.py` (Railway image). On this VPS the binary lives at `/opt/ffmpeg/ffmpeg` — for local manual runs export `PATH=/opt/ffmpeg:$PATH`.

