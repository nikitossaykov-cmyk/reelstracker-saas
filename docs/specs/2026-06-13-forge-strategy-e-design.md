# Forge — Strategy E (Face Replace) — design

**Status:** draft, awaiting owner review
**Date:** 2026-06-13
**Author:** Claude (брейн-шторм с владельцем)
**Related:** PR #21–#30 (Strategy D plumbing), `fix/forge-strategy-bd-empty-player` (frontend result fix)

---

## 1. Summary

Add a fifth Forge strategy `E` whose job is to take a donor video that
already has the right structure / pacing / product shots, and replace
the on-screen woman with a locked AI-fictional persona of the brand's
choosing. The persona is created once, lives in a per-user library, and
is reused across every E-strategy remix.

Two replacement modes are exposed in the same UI:

- **Mode 1 — face only.** Donor's body, outfit, hands, environment,
  audio, framing — all preserved. Only the face region is swapped.
  Fast and cheap. Implementation: hosted face-swap model on Replicate
  / fal.ai.
- **Mode 2 — face + body.** Donor provides motion / pose / lipsync
  only; face, body, outfit are regenerated to match the persona.
  Slower, costlier, higher visual control. Implementation: existing
  Wan-2.2 Animate workflow on the owner's RunPod pod
  (`/opt/tg-bot/tools/wan_clone.py`).

Both modes flow through the same Forge UI + DB + media-storage paths
already wired up for Strategies A–D, so this is additive — no behavior
changes for existing strategies.

---

## 2. Goals & non-goals

### Goals

- One new tab `E · Замена девушки` in `/forge`, parity with A/B/C/D
  (URL input, params, submit, poll, result).
- Persona library: list / create / delete personas per user.
  Persona = fictional identity (name + text bio) materialised into one
  canonical face-reference image and a small gallery.
- Reuse the storage / streaming / faststart / R2 plumbing already
  proven for Strategies A–D — no new media path.
- Mode 2 uses the existing Wan workflow, not a re-implementation.

### Non-goals (MVP)

- LoRA / DreamBooth fine-tuning of personas. The face reference image
  is the persistence boundary; nothing trains.
- Multi-subject donors. If donor has two women on screen, we swap
  whichever face the model picks first; explicit multi-face routing is
  out of scope.
- Voice cloning / lipsync rewrite. Audio is donor-original. Audio
  rewrites belong to Strategy A or a future Strategy F.
- Persona marketplace, social personas, sharing between users. Each
  persona belongs to exactly one user.
- Batch / queue UI for many videos at once. One submit = one result,
  same as A–D.

---

## 3. User-facing UX

### 3.1 Forge tab "E · Замена девушки"

Inputs:

- `source_url` (URL of donor video — same input/validation as D)
- `persona_id` — `<select>` populated from `/api/personas`, plus
  `+ создать новую` button that opens the persona-create modal inline
- `mode` — segmented control: `Face only` / `Face + body`
- Submit: `🎬 Заменить`

Progress and result reuse the same components as Strategy D —
`pollStrategyC(gv_id)` loop and `showStrategyBResult` handler (now
fixed in `fix/forge-strategy-bd-empty-player`).

### 3.2 Persona library page `/personas.html`

- Grid of persona cards. Each card: canonical face thumbnail, name,
  bio first line, `Использовать в Forge` button, `…` menu (rename,
  delete).
- Top-right: `+ Новая персона`.
- Empty state: short copy explaining what a persona is and why locking
  one matters for brand consistency.

### 3.3 Persona create modal

Form fields:

- `name` (≤64 chars) — internal label, also used in download filenames
- `bio` (≤512 chars) — text description of the fictional persona, e.g.
  *"блондинка 23 лет, прохладный светлый тон кожи, голубые глаза,
  мягкие черты, ровная кожа, лёгкий макияж, студийный свет"*
- Style hint (optional dropdown): `editorial / lifestyle / studio /
  street` — appended to the prompt as a style modifier
- Submit: `Сгенерировать (~$0.20)`

Flow:

1. POST `/api/personas/` returns `persona_id` and `status=generating`.
2. UI shows a spinner card; polls `/api/personas/{id}` every 5 s.
3. On ready: backend has produced 4 candidate face-shots from the same
   PuLID seed (front / 3-quarter / soft smile / serious). UI shows the
   4 thumbs in a chooser. User clicks one → POST
   `/api/personas/{id}/canonical` with the chosen index. Server saves
   the chosen image URL as `canonical_face_url`. Persona becomes usable.
4. If safety rejects the bio → retry once with a defensively-generic
   prompt (the same one-retry-on-safety pattern documented for
   `gpt-image-1` in the project's skill notes — generalises to any
   moderated image API); if still rejected, surface an actionable
   Russian error naming the field most likely to be the trigger.

---

## 4. Architecture

```
                       ┌──────────────────────────┐
   /forge (E tab) ───▶ │   POST /api/forge/start  │
   /personas      ───▶ │   strategy=E             │
                       └──────────────┬───────────┘
                                      │
                                      ▼
                       ┌──────────────────────────┐
                       │   forge_e_service.start  │
                       │   (sync: validate +      │
                       │    enqueue, returns      │
                       │    gv_id immediately)    │
                       └──────────────┬───────────┘
                                      │
                                      ▼
                            ┌─────────────────────┐
                            │  generated_videos   │
                            │  row, status=queued │
                            │  strategy=E, mode=N │
                            └──────────┬──────────┘
                                       │
                                       ▼ (SKIP LOCKED)
                       ┌───────────────────────────┐
                       │  forge_e_worker (loop)    │
                       │                            │
                       │  1. download donor         │
                       │     (yt-dlp + Apify)       │
                       │  2. resolve persona →      │
                       │     canonical_face_url     │
                       │  3a. mode=1 → Replicate    │
                       │      face-swap API         │
                       │  3b. mode=2 → wan_clone.py │
                       │      → RunPod pod          │
                       │  4. ensure_faststart()     │
                       │  5. upload R2              │
                       │  6. status=ready,          │
                       │     media_url=…            │
                       └────────────┬──────────────┘
                                    │
                                    ▼
                       front polls /api/media/diag/{gv_id}
                       → showStrategyBResult({media_url, gv_id})
```

A separate `personas` subsystem powers persona creation, independent
of Forge runtime:

```
   /personas (UI) ───▶ POST /api/personas/
                                │
                                ▼
                       persona_service.create_async
                                │
                                ▼
                       personas row, status=generating
                                │
                                ▼ (worker, separate)
                       Replicate PuLID-Flux × 4 seeds
                                │
                                ▼
                       gallery_json populated,
                       status=awaiting_canonical
                                │
                                ▼
                       user picks one → canonical_face_url set,
                       status=ready
```

**Key architectural note.** Per memory entry "Forge tech insights —
Fable 5 ревью 2026-06-12", priority 1 is moving the worker out of the
web process. Strategy E's worker is the first concrete piece designed
this way:

- `app/services/forge_e_service.py` only enqueues + returns.
- `app/workers/forge_e_worker.py` polls the `generated_videos` table
  using `SELECT ... FOR UPDATE SKIP LOCKED` for one E-mode row at a
  time and runs the pipeline.
- The worker is the same Python process as the rest today, behind a
  `WORKER_FORGE_E=1` env flag — but the boundary is drawn cleanly so it
  can move to a dedicated Railway service later without API churn.

---

## 5. Components

### 5.1 Persona Library

#### Data model

New table `personas`:

| col | type | notes |
|---|---|---|
| `id` | int PK | |
| `user_id` | int FK → users | indexed |
| `name` | varchar(64) | |
| `bio` | varchar(512) | |
| `style_hint` | varchar(32) nullable | |
| `status` | enum(`generating`, `awaiting_canonical`, `ready`, `failed`) | |
| `canonical_face_url` | text nullable | R2 proxy URL (`/api/media?key=…`) |
| `gallery_json` | jsonb | array of `{url, seed, index}` |
| `error_message` | text nullable | |
| `created_at` | timestamp | |
| `ready_at` | timestamp nullable | |

Constraints: unique `(user_id, name)`.

#### API

| method | path | purpose |
|---|---|---|
| GET | `/api/personas/` | list current user's personas |
| POST | `/api/personas/` | create — body `{name, bio, style_hint?}` → 202 + persona row in `generating` |
| GET | `/api/personas/{id}` | poll status, returns gallery when `awaiting_canonical` |
| POST | `/api/personas/{id}/canonical` | body `{gallery_index: int}` → sets `canonical_face_url`, status → `ready` |
| DELETE | `/api/personas/{id}` | soft delete (don't break historical generations referencing it) |

#### Persona generation

Implemented in `app/services/persona_service.py`:

1. Validate inputs.
2. Build prompt with defensive moderation-friendly language
   (`fictional generic content creator, no real-person likeness, no
   logos of existing brands` — per `gpt-image-1-safety-fallback`).
3. Call Replicate model `lucataco/pulid-flux` (or equivalent — see
   §13 Open questions) 4× with fixed face-reference style and varied
   seeds → 4 candidate images.
4. NSFW check + face-detected check on each. Drop rejected slots
   (allow 1–4 slots).
5. Upload images to R2 under `users/{uid}/personas/{pid}/{seed}.png`.
6. Update row: `gallery_json`, status → `awaiting_canonical`.
7. On safety rejection of bio → one defensive retry; if still rejected
   → status `failed`, `error_message` names the likely-offending field.

### 5.2 Strategy E pipeline

#### Forge entrypoint

`/api/forge/start` accepts `strategy=E` with body:

```json
{
  "strategy": "E",
  "source_url": "https://…",
  "persona_id": 12,
  "mode": 1
}
```

Validation:
- `persona_id` belongs to user, status=`ready`.
- `source_url` passes the same regex/length checks as A–D.
- `mode ∈ {1, 2}`.
- User has the API keys required for the chosen mode (see §5.4).

Response: `{strategy: "E", gv_id: 123, next_step: "poll /api/media/diag/{gv_id}"}`.

A `generated_videos` row is inserted with `status=queued`, `provider`
set to either `replicate` (mode 1) or `runpod_wan` (mode 2), and new
columns `persona_id` and `mode`.

#### Worker

`app/workers/forge_e_worker.py` — single loop:

```python
while True:
    with locked_row(strategy="E", status="queued") as gv:
        if gv is None:
            sleep(2); continue
        gv.status = "running"
        gv.started_at = now()
        try:
            run_e_pipeline(gv)
            gv.status = "ready"
            gv.completed_at = now()
        except RetryableError as e:
            gv.status = "queued"; gv.retry_count += 1; gv.error_message = str(e)
        except Exception as e:
            gv.status = "failed"; gv.error_message = str(e)
```

`started_at` is required by the §10 sweep that recovers rows stuck in
`running` after a worker restart.

`run_e_pipeline` is the body of the diagram in §4.

### 5.3 Mode 1 — face only (Replicate)

`app/services/forge_e_mode1.py`:

- Load donor mp4 to temp dir (reuse `download_source_video`).
- Upload donor to a temporary public URL (Replicate requires URLs)
  via the existing R2 upload + presigned-GET helper, ttl 1h.
- Upload `persona.canonical_face_url`-targeted image likewise.
- POST to Replicate `cdingram/face-swap` (or `lucataco/faceswap` —
  see §13). Poll until result URL.
- Download result, `ensure_faststart()`, upload to R2 at
  `users/{uid}/forge_e/{hex}.mp4`, set `media_url`.

Expected end-to-end: 30–120 s, $0.02–0.05 / video. User pays via own
Replicate API key, stored under `user.replicate_api_key` (new field, §6).

### 5.4 Mode 2 — face + body (RunPod Wan-Animate)

`app/services/forge_e_mode2.py`:

- Wraps the existing `/opt/tg-bot/tools/wan_clone.py` CLI.
- Before invoking: check `/root/.wan_pod_info` exists and ssh port is
  reachable. If not, call `pod_start.sh` and wait up to 120 s for the
  pod to come up.
- Invoke `wan_clone.py <donor.mp4> <persona_face.png> --out result.mp4`.
  Capture stdout / stderr.
- On success: `ensure_faststart()`, upload to R2.
- Per-user RunPod cost is currently borne by the platform (owner's
  pod). Track in `cost_runpod_seconds` on the row so we can derive
  per-video cost honestly later (per memory: "cost_usd в БД ДО
  биллинга").
- **Auto-stop.** A cron-style task in the worker process runs every
  minute: if no E-mode-2 row has been `RUNNING` in the last 10 min and
  pod is up, call `pod_stop.sh`. The 10 min idle window is a knob in
  config.
- **Queue semantics.** Multiple Mode 2 requests are serialised
  through `SELECT FOR UPDATE SKIP LOCKED` (only one E-2 row in
  `RUNNING` state at a time, configurable).

### 5.5 RunPod orchestration (shared helper)

New module `app/services/runpod_pod.py`:

- `ensure_pod_up()` — idempotent, returns connection info.
- `stop_pod_if_idle(max_idle_minutes)` — for the cron tick.
- `pod_alive_seconds()` — read for cost attribution.
- Reads / writes `/root/.wan_pod_info` (on Railway: this is ephemeral —
  see §13 open question on persistence).

---

## 6. Data model changes

### New columns on `users`

| col | type | notes |
|---|---|---|
| `replicate_api_key` | text nullable | encrypted at rest (same helper as other API keys) |

### New columns on `generated_videos`

| col | type | notes |
|---|---|---|
| `persona_id` | int FK → personas nullable | only set for strategy E |
| `mode` | smallint nullable | only for E: 1 or 2 |
| `cost_runpod_seconds` | float nullable | mode 2 only |
| `cost_replicate_usd` | numeric(10,4) nullable | mode 1 only |

These piggyback on the broader `cost_usd` migration already on the
priority list (memory: "cost_usd в БД ДО биллинга") — landed here as
a first concrete instance of it.

### New table `personas`

Already specified in §5.1.

### Alembic migration

One revision file: `add_personas_and_forge_e_columns.py`. Covers
table + column additions, no destructive changes.

---

## 7. API surface (full list)

```
GET    /api/personas/
POST   /api/personas/
GET    /api/personas/{id}
POST   /api/personas/{id}/canonical
DELETE /api/personas/{id}
POST   /api/forge/start            (existing — extend to strategy=E)
GET    /api/media/diag/{gv_id}     (existing — works as-is)
```

No changes to existing endpoints' shapes; `forge/start` just learns a
fifth strategy value.

---

## 8. Frontend integration

### Files touched

- `static/forge.html` — add `E` tab + form + submit handler →
  `startForge()` already routes by `activeStrategy`; extend it.
- `static/personas.html` — new page (mostly cribbed from existing
  forge layout for visual consistency).
- `static/js/personas.js` — small module, persona list + create modal.
- `app/main.py` — route `GET /personas.html` to the static asset.

### State management

No new framework. Plain DOM updates as elsewhere in the project. Both
pages share the existing `authFetch` helper.

---

## 9. Cost model

| step | cost / call | who pays |
|---|---|---|
| Persona create (Replicate PuLID × 4) | ~$0.20 | user (own Replicate key) |
| Mode 1 video (Replicate face-swap) | ~$0.02–0.05 | user |
| Mode 2 video (RunPod Wan time) | ~$0.05–0.20 (≈ 2–10 pod-minutes ÷ amortisation) | platform today; user-token path in §13 |

The platform's exposure on Mode 2 is a deliberate MVP shortcut: it
unblocks owner from validating the use case without first wiring up
a billing flow. The data model is ready for per-user cost
attribution from day one.

---

## 10. Error handling

### Persona create

- Replicate timeout / 5xx → one retry, then row → `failed`.
- Safety rejection → one defensive-prompt retry (skill
  `gpt-image-1-safety-fallback`), then `failed` with field-named
  Russian error.
- Face-detect failure on all 4 candidates → `failed` with copy
  "Не удалось разобрать сгенерированные лица, попробуй уточнить bio
  (возраст, тип внешности, освещение)".

### E pipeline

- Donor download fails → mirror Strategy D's existing error surface
  (Apify token check etc).
- Mode 1 Replicate rejection → if safety, reject before charging the
  user a second call.
- Mode 2 pod unreachable after 120 s → row → `failed`, surface
  `pod_start.sh` failure verbatim. Owner-only debug field in DB.
- Faststart failure → log + don't block; the streaming proxy still
  works for the common case but Safari may show 0:00 (per the
  `mp4-faststart-streaming` skill).
- R2 upload failure → retryable (3 attempts with backoff), then
  `failed`.

### Worker resilience

- Worker stop mid-pipeline (Railway redeploy) → the row stays in
  `RUNNING` with a stale `started_at`. On worker start, sweep any
  E-rows in `RUNNING` older than 30 min back to `queued` with
  `retry_count++`. After 3 retries → `failed`. This is the standard
  "сброс зависших RUNNING" pattern but applied per-row, not global.

---

## 11. Testing strategy

### Unit

- `forge_e_service.start` — input validation, row creation, persona
  ownership check, mode handling.
- `persona_service` — prompt construction (defensive language present),
  safety-retry behaviour, candidate filtering.
- `runpod_pod` — pod lifecycle helpers with subprocess mocked.

### Integration (DB-backed)

- Persona happy path: create → poll → canonical → ready.
- Persona safety rejection: bio with `young teenager` → expect
  `failed` with field hint.
- Forge E queue: enqueue 2 rows, assert worker processes them
  sequentially under `FOR UPDATE SKIP LOCKED`.

### Manual / staging

- Strategy E end-to-end with a synthetic donor video and a generated
  persona — verify the result plays in the fixed
  `showStrategyBResult` path (also covers the PR
  `fix/forge-strategy-bd-empty-player`).
- RunPod cold-start path: stop pod, submit Mode 2 job, observe
  autostart + result + autostop.

### Out of scope for MVP test

- Adversarial donors (multi-face, occluded face) — caught as failure,
  not handled gracefully.
- Cost attribution end-to-end correctness — covered by per-row
  fields, will land properly with the broader billing work.

---

## 12. Out of scope (explicit non-goals revisited + future)

| later | not now because |
|---|---|
| LoRA training per persona | YAGNI until the face-ref pipeline proves not-good-enough |
| Wan / SDXL self-hosted Mode 1 | Replicate is fine until volume forces a migration |
| Persona marketplace / sharing | adds account-trust + abuse problems before product-market fit |
| Tariff / billing for Mode 2 | wait until users do this twice unprompted |
| Multi-face / multi-subject donors | edge case for parfume ads, can defer |
| Lipsync rewrite tied to persona voice | belongs to a "voice" subsystem, not face replace |

---

## 13. Open questions

1. **Exact Replicate models.** `cdingram/face-swap` vs
   `lucataco/face-swap` vs `omniedgeio/face-swap` for Mode 1;
   `lucataco/pulid-flux` vs `zsxkib/pulid-flux` for personas. Pick on
   eval pass before owner spends real money.
2. **`/root/.wan_pod_info` persistence on Railway.** Railway containers
   have ephemeral disks. Either store pod info in DB (preferred) or
   restore from RunPod API on worker boot.
3. **Per-user RunPod tokens.** Memory note "случайно гениальный ход —
   per-user Apify-токен" suggests Mode 2 should also accept a
   per-user RunPod token longer-term. MVP uses owner's. Track this for
   v2.
4. **Persona deletion semantics.** Soft-delete only? Historical
   generated_videos.persona_id should not be orphaned. Default to
   soft-delete with `deleted_at`.
5. **Canonical face vs gallery.** Should we let the user keep all 4
   candidates and pick per-video, or freeze to one? MVP: freeze one,
   rest stay as gallery for inspection.

---

## 14. Files this design will touch

```
docs/specs/2026-06-13-forge-strategy-e-design.md        (this)
alembic/versions/<rev>_add_personas_and_forge_e.py      (new)
app/models/persona.py                                   (new)
app/models/generation.py                                (add persona_id, mode, cost cols)
app/models/user.py                                      (add replicate_api_key)
app/api/personas.py                                     (new)
app/api/forge.py                                        (extend Strategy literal + branch)
app/services/persona_service.py                         (new)
app/services/forge_e_service.py                         (new, enqueue only)
app/services/forge_e_mode1.py                           (new)
app/services/forge_e_mode2.py                           (new — wraps wan_clone.py)
app/services/runpod_pod.py                              (new)
app/workers/forge_e_worker.py                           (new)
app/main.py                                             (register personas router, /personas.html route)
static/personas.html                                    (new)
static/forge.html                                       (E tab + form)
tests/test_persona_service.py                           (new)
tests/test_forge_e_service.py                           (new)
tests/test_runpod_pod.py                                (new)
```

Wan workflow template (`wan_workflow_template.json`) and pod scripts
(`pod_start.sh`, `pod_stop.sh`) currently live outside the repo under
`/opt/tg-bot/tools/`. Decision deferred to implementation: copy them
into `infra/wan/` in this repo, or keep external and read by path
from env. (Lean: copy in — they are part of the contract this code
depends on.)
