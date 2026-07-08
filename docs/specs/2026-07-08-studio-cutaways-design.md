# Studio Action Cutaways — Design

**Date:** 2026-07-08
**Status:** approved by Nick (TG, «2» → «Да»)
**Context:** Nick's feedback on the first prod Studio reel: the script promised
«сейчас я открою» but the talking head never does anything. v36 solved this
with two short action inserts (cap-off, spray) between narrative part 1 and
the reaction. This spec automates that recipe inside the Studio pipeline.

## Goal

A Studio job can optionally include two AI-generated action cutaways —
«снимает крышку» and «пшикает на запястье» — spliced into the talking-head
take at the natural pause after the script's promise phrase.

## Proven constraints (from v36 / wc_full_pipeline notes)

- Stills via **nano-banana-pro** with two image refs (portrait + real product
  photo) keep the bottle identical across frames.
- **Kling i2v works for spray**, fails for sniff (renders drinking/kissing).
  We do cap-off + spray only; no sniff insert.
- Kling STANDARD is ~11× cheaper than PRO with acceptable b-roll quality.
- Inserts must land inside a **voiceover pause** so no speech is lost.

## Pipeline change

```
PENDING → PORTRAIT → VOICEOVER → LIPSYNC → CUTAWAYS → ASSEMBLE → JUDGE → READY
                                            (new, optional, non-blocking)
```

CUTAWAYS runs when `job.cutaways_enabled` (default true). Any failure inside
the stage is **non-blocking**: log, keep whatever clips succeeded, continue to
ASSEMBLE. A reel without inserts still ships (same philosophy as judge).

## Data model (`app/models/studio_job.py` + lightweight migrations)

New columns on `studio_jobs` (added to `run_lightweight_migrations()` in
`app/main.py` — status column is `String(24)`, so the new enum value is free):

| column | type | default |
|---|---|---|
| `cutaways_enabled` | BOOLEAN NOT NULL | TRUE |
| `cap_still_key` | TEXT NULL | |
| `spray_still_key` | TEXT NULL | |
| `cap_clip_key` | TEXT NULL | |
| `spray_clip_key` | TEXT NULL | |

`StudioStatus` gains `CUTAWAYS = "cutaways"` between LIPSYNC and ASSEMBLE.
Retry (`POST /jobs/{id}/retry`) clears all four new keys like other stage keys.

## New module: `app/services/strategy_single_take/cutaways.py`

- `build_cutaway_still_prompt(kind, product_name, brand)` — kind ∈
  {"cap_off", "spray"}. Text mirrors the proven wc_spritz_clip.py prompt
  structure: photorealistic, SAME woman as in the first reference image
  (portrait), the EXACT bottle from the second reference image (product
  photo), label text readable, vertical 9:16, UGC aesthetic.
  - cap_off: both hands at chest height, lifting the matte black cap off the
    bottle, cap just separated, eyes on the bottle.
  - spray: index finger pressing the pump, fine mist visible against the
    light, wrist/neck target, eyes softly closed.
- `generate_cutaway_still(*, portrait_bytes, product_bytes, kind,
  product_name, brand, replicate_api_key) -> (result, cost)` —
  `google/nano-banana-pro`, `image_input=[portrait, product]`, 9:16 jpg,
  cost $0.15 (same client/data-URI helpers as portrait.py).
- `animate_cutaway(*, still_bytes, kind, replicate_api_key) -> (result, cost)`
  — `kwaivgi/kling-v2.1-standard`, 5s, image start frame. Prompt: one subtle
  continuous motion of that action only; negative: drinking, kissing,
  mouth-to-bottle. Cost ~$0.25.
- `CutawayError(Exception)`.

## Worker (`app/workers/studio_worker.py`)

New stage block after LIPSYNC, resume-safe like the others:

- Skip entirely unless `j.cutaways_enabled`.
- For each kind in ("cap_off", "spray"):
  - if `<kind>_clip_key` present → skip (resume).
  - still: generate (needs `portrait_key` blob + product photo blob), upload
    `.../cutaway-{kind}-{hex}.jpg`, save `<kind>_still_key`, add cost.
  - clip: animate still, upload `.../cutaway-{kind}-{hex}.mp4`, save
    `<kind>_clip_key`, add cost.
  - On `ReplicateSafetyError` / `ReplicateError` / any Exception → log
    warning, continue (clip missing = insert skipped; if the still exists but
    animation failed, assemble falls back to a static 1.2s clip from the
    still).
  - `ReplicateTransientError` re-raises (run_loop retry), same as portrait.

`_mark(db, j, StudioStatus.CUTAWAYS)` at stage start.

## Assemble changes

New pure helpers (unit-testable, no ffmpeg):

- `captions.pick_insert_gap(spans, total) -> float | None` — midpoint of the
  **longest gap between speech spans** whose midpoint falls within
  20%–85% of `total`; None if the longest such gap < 0.5s. This is where the
  body is split (v36's gap was 2.5s — dominant by construction, the script
  prompt demands a pause after the promise phrase).
- `captions.shift_captions(aligned, split_at, inserts_seconds)` — sentences
  with `start >= split_at` shift right by `inserts_seconds`.

ffmpeg helpers (`assemble.py`):

- `cut_clip(src, dst, start, end=None)` — re-encode cut with VF_NORMALIZE
  (reuses normalize settings so concat stays uniform).
- `still_to_clip(jpg, dst, seconds)` — static fallback: loop image
  `CUTAWAY_SECONDS=1.2` with silent audio.
- `normalize_clip` gains guaranteed audio: if the source has no audio stream
  (Kling clips are silent), inject `anullsrc` so `concat_clips` stays valid.

`_assemble(j, ...)` new flow when insert clips exist AND
`pick_insert_gap` finds a pause in the voiceover:

```
body = normalize(lipsync)
split = pick_insert_gap(speech_spans(vo), vo_total)
body_a = cut_clip(body, 0, split); body_b = cut_clip(body, split)
inserts = [trim(normalize(clip), 1.2s) for clip in (cap, spray) if present]
raw = concat(hook? + body_a + inserts + body_b)
captions: aligned (VO timeline) → shift_captions(split, sum(inserts))
          FIRST (split is in VO timeline), THEN shift all by hook_seconds
polish unchanged
```

If no gap found or no clips → current behaviour (straight body), inserts
silently skipped.

## Script prompt (`script.py`)

`build_studio_script_prompt(..., cutaways: bool)`:

- `cutaways=True`: part 1 must END with exactly one short promise phrase
  («Сейчас открою…» / «Давайте попробуем…») followed by an explicit long
  pause; all other action promises stay banned; part 2 is pure reaction.
- `cutaways=False`: current full ban (PR #68 text).

`POST /api/studio/script` request model gains `cutaways_enabled: bool = True`.

## API (`app/api/studio.py`)

- `POST /jobs/` form field `cutaways_enabled: bool = Form(True)`.
- Response model exposes `cutaways_enabled` and the four new keys.
- Retry clears `cap_still_key, spray_still_key, cap_clip_key, spray_clip_key`.
- `/api/media` allowlist: add the four new key columns.

## UI (`static/studio.html`)

- Checkbox «вставки-действия (открыть + пшик)», checked by default; sent to
  both `/script` and job create.
- STAGES/ORDER add `['cutaways', 'Вставки']` between Липсинк and Сборка;
  chips logic unchanged (key presence check uses `cap_clip_key`).

## Cost

Per reel with cutaways: +2 stills ($0.30) + 2 Kling std (~$0.50) ≈ **+$0.80**
worst case, ~$1.70 total. (Estimate given to Nick was +$0.55; Kling std
pricing to be confirmed on the first run — log actuals.)

## Testing

- Pure: `pick_insert_gap` (dominant gap, edge windows, no-gap → None),
  `shift_captions`, prompt builders (promise allowed only with
  cutaways=True), cutaway still/animate prompt content.
- Worker: monkeypatched cutaway generators — happy path (keys + cost),
  animation failure → still fallback path selected, full stage failure →
  job still reaches READY.
- API: create with `cutaways_enabled=false` → stage skipped; retry clears new
  keys; media allowlist covers new keys.
- Existing 65 tests stay green.

## Out of scope

- Sniff insert (Kling can't).
- Configurable insert count/kinds in UI.
- Re-generating only cutaways without full retry.
