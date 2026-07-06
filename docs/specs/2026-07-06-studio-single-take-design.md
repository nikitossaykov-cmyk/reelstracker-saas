# Studio — single-take UGC reel builder (POC)

**Date:** 2026-07-06 · **Status:** approved by Nick (TG) · **Scope:** POC, demo-able

## Goal

Productize the proven WC single-take pipeline (vault iterations v31–v36, judge 8–9/10,
$1–2/reel) as a polished page inside reelstracker-saas: form in → finished reel out,
with live stage progress and an auto-QC score.

## What exists vs what's new

Already in repo (reused as-is): JWT auth, R2 + media proxy, worker pattern
(polling threads), credit ledger, `strategy_makeugc/voiceover.py` (ElevenLabs),
`strategy_makeugc/lipsync.py` (Pruna p-video-avatar — same model as vault),
HEIC-safe photo upload UI patterns from `makeugc.html`.

New (ported from vault scripts `/opt/vault/Projects/ideas/_assets/wc_single_take/`):

| Stage | Source recipe | Notes |
|---|---|---|
| portrait | nano-banana-pro (google/nano-banana-pro on Replicate), real product photo as 2nd image_input, misspelling ban-list in prompt | extreme close-up ASMR framing (mic prop) or standard framing |
| voiceover | eleven_v3, stability 0.30 / style 0.85; `[whispers]` per sentence when ASMR toggle on | reuse quota tracking |
| lipsync | Pruna (existing module) | |
| captions | word-by-word ASS subs, DejaVu Sans Bold 58, Alignment 2, MarginV 215; sentence spans from silencedetect (-26dB:d=0.07 for whisper, -25dB:d=0.12 normal), words proportional to len(word)+1 | burned before polish |
| assemble | ffmpeg concat: optional hook clip (user upload, normalized 720×1280/30) + talking take | no gesture inserts in POC |
| polish | body setpts/atempo 1.05 (hook untouched) + `noise=alls=5:allf=t`; NO handheld shake | |
| judge | Gemini video QC (port of `/opt/tg-bot/tools/reel_judge.py`), flash-lite fallback on 429/503 | score + verdict badge; non-blocking (reel is READY even if judge fails) |

## Architecture

- **Model:** `StudioJob` (new table `studio_jobs`), mirrors `MakeUGCJob` shape:
  inputs (product_name, brand, price_rub, dupe_price_rub, product_image_keys JSON,
  script_text, voice_style enum normal|asmr, captions_enabled bool, hook_video_key
  nullable), stage output keys (portrait_key, voiceover_key, lipsync_key, output_key),
  judge_score int nullable, judge_report JSON nullable, status, error_message,
  cost_usd, timestamps.
  Status flow: `PENDING → PORTRAIT → VOICEOVER → LIPSYNC → ASSEMBLE → JUDGE → READY / FAILED`.
- **Pipeline package:** `app/services/strategy_single_take/` — `portrait.py`,
  `captions.py`, `assemble.py`, `judge.py`; voiceover/lipsync imported from
  `strategy_makeugc` (thin wrappers where signatures differ). Script autogen reuses
  `strategy_makeugc/script.py` with an ASMR-aware template tweak.
- **Worker:** `app/workers/studio_worker.py` — same polling-thread pattern as
  `makeugc_worker.py`, one job at a time, writes stage keys + cost_usd as it goes.
- **API:** `app/api/studio.py` — `POST /api/studio/jobs` (multipart: fields + photos +
  optional hook video), `GET /api/studio/jobs`, `GET /api/studio/jobs/{id}`,
  `POST /api/studio/jobs/{id}/retry`, `POST /api/studio/script` (script autogen for
  the textarea). All behind JWT like makeugc.
- **UI:** `static/studio.html` — glassmorphism like the rest; left column = form
  (product, photos drag-drop, script textarea + «сгенерировать» button, voice-style
  toggle, captions toggle, hook upload), right column = job cards: stage progress
  chips (Портрет → Озвучка → Липсинк → Сборка → QC), video player on READY,
  judge score badge, cost, «перегенерить» button. Route `/studio` in main.py.

## Keys / config

Server-side keys for POC (env): `REPLICATE_API_TOKEN` (nano-banana + Pruna),
ElevenLabs (existing per-user or env fallback — same rule as makeugc), new
`GEMINI_API_KEY` for judge. Per-user keys: later, out of scope.

## Costs

Logged to `StudioJob.cost_usd` per stage (portrait ~$0.15, TTS ~$0.05,
Pruna ~$0.74, judge $0). No credit debiting in POC — display only.

## Error handling

Stage failure → status FAILED + error_message, artifacts of completed stages kept
so retry can resume from the failed stage (POC: retry restarts whole pipeline,
resume is a follow-up). Judge failure (quota) → job still READY, badge «QC недоступен».

## Testing

Unit: captions ASS generator (word timing math), silencedetect parser, judge fallback
rotation (mocked HTTP). Service: pipeline orchestration with mocked Replicate/eleven
(pattern from test_forge_e_service.py). Manual: full run against real APIs with a
dose product photo, verify in browser.

## Out of scope (POC)

Payments/credit debiting, autoposting, gesture inserts (cap-off/spray Kling clips),
per-user API keys, macro label insert, resume-from-stage retry.
