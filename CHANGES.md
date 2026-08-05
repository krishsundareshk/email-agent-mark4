# CHANGES.md — v3 Rewrite (Priority → Relationship, Memory, Dual Providers, Agentic Tool-Calling)

This follows the build order from `pipeline_changes.md`'s "Suggested build
order" section. Each phase lists the files touched and what changed.
Everything below is implemented and passing (`pytest tests/` — 31 tests;
Alembic migration verified end-to-end; frontend `npm run build` verified).

---

## Phase 1 — Schema changes + Stage 0 filter

**New:**
- `app/agents/models/enums.py` — rewritten. `Priority` removed entirely.
  Added `RelationshipLabel`, `ConfidenceTier`, `TrustTier`,
  `EmailProviderEnum`, `CalendarProviderEnum`, `BatchRunStatus`.
- `app/agents/models/classified_email.py` — rewritten. Dropped
  `summary`/`subject`/`priority`. Added `relationship`, `department`,
  `is_meeting`, `confidence_tier`, `self_reported_certainty`,
  `reflection_agreement`, `thread_id`. Added `EvidenceVector` (Stage 3 input).
- `app/agents/models/sender_memory.py`, `thread_memory.py`,
  `label_correction.py` — new Pydantic models for long-/short-term memory
  and the correction audit log.
- `app/db/models.py` — rewritten. `classified_emails` drops
  `summary`/`subject`/`priority`/`classification_confidence`/
  `flagged_for_review`; gains `relationship`, `department`, `is_meeting`,
  `confidence_tier`, `reflection_agreement`. New tables: `sender_memory`,
  `global_label_centroids`, `thread_memory`, `label_corrections`.
  `meeting_cards` gains `resolution`/`resolved_at`/`calendar_event_id` for
  the content-purge lifecycle. `batch_run_logs` gains `stage0_resolved`.
  `users` gains `calendar_provider`, `org_domains`.
- `app/agents/tools/stage0_bulk_filter.py` — new. Deterministic
  RFC-header-based (`List-Unsubscribe`, `Precedence`) Promotional
  detection. No LLM call.
- `app/providers/email/base.py` — `EmailObject` gains `thread_id`,
  `list_unsubscribe`, `list_unsubscribe_post`, `precedence`.
- `alembic/`, `alembic.ini` — new. One clean-break revision
  (`v3_clean_break`) per the migration note: drops the old shape of
  `users`/`classified_emails`/`meeting_cards`/`batch_run_logs`/
  `processed_emails` and recreates everything from current ORM metadata.
  `app/db/database.py`'s old sqlite `ALTER TABLE` self-migration hack removed.

---

## Phase 2 — Merged Stage 1 reasoning call (no Priority, no memory/tools yet)

**Removed:** `app/agents/tools/classify_email.py` (retired — folded into
`reasoning_engine.py`), its test `tests/tools/test_classify_email.py`.

**New:** `app/agents/tools/reasoning_engine.py` — the merged Stage 1
classifier. No Priority output anywhere in its schema or prompt.

*(In this repo, phases 2 and 3 below were implemented together as one
`reasoning_engine.py`, since the tool-calling wiring and the classification
schema are tightly coupled in a single Observe/Reason/Act/Reflect/Finalize
function — see Phase 3.)*

---

## Phase 3 — Memory stores + tool-calling wiring

**New:**
- `app/agents/memory/sender_memory_store.py` — `get_sender_memory`,
  `update_sender_memory`, `apply_human_correction`, plus the combined-
  distribution fusion (`get_combined_distribution`: count-based prior ×
  cosine-similarity likelihood against label centroids, with global
  per-label centroid fallback for cold-start senders).
- `app/agents/memory/thread_memory_store.py` — `get_thread_memory`,
  `update_thread_memory`, `expire_stale_threads`.
- `app/agents/tools/reasoning_engine.py` — Observe (pulls sender memory +
  thread memory + combined distribution + Stage 0 signal summary +
  regex-extracted candidate links) → Reason/Act (LLM call, tentative
  decision) → Reflect (second LLM call, checks the tentative decision
  against memory) → Finalize (calls `apply_label` + both memory-update
  tools). This is genuine multi-turn tool use around a plain completion
  API, not a single classification call.

---

## Phase 4 — `embedding_adapter` + label centroids + combined distribution

**New:**
- `app/agents/embedding/embedding_adapter.py` — wraps whichever LLM
  provider handles embeddings via `EMBEDDING_PROVIDER` (falls back to
  `LLM_PROVIDER`), independent of the reasoning provider, per the "pin
  it" recommendation in specs v3 §9.1.
- `app/agents/tools/llm_client.py` — added `embed()` (Ollama `/api/embeddings`,
  OpenAI `/v1/embeddings`, Gemini `embedContent`) and `chat()` (multi-turn
  primitive used by the reasoning loop). Constructor now accepts a
  `provider` override so the embedding client can be pinned independently.
- Running-mean centroid updates live in `sender_memory_store._update_centroid`
  / `_update_global_centroid` — no per-email embedding is ever persisted,
  only the centroid it folds into (specs v3 §5.3, §6).

---

## Phase 5 — Confidence engine

**New:** `app/agents/tools/confidence_engine.py` — `compute_confidence_tier()`,
the five-signal rubric from specs v3 §4 (self-reported certainty,
reflection agreement, memory consistency, structural corroboration,
ambiguity margin) → `ConfidenceTier`. Suspicious is always forced to
`needs-review` regardless of other signals. Replaces the old single-float
`classification_confidence` + `flagged_for_review` pair entirely.

Wired into `reasoning_engine.py`'s Finalize step.

---

## Phase 6 — `apply_label` as a real tool + reconciliation job

**New:**
- `app/agents/tools/apply_label_tool.py` — the dual-write tool: provider
  write (`EmailProvider.apply_label`) + local DB write, first-class and
  loud about partial failure (never silent).
- `app/providers/email/base.py` — added abstract `apply_label()` and
  `get_current_label()` to the interface, plus `ApplyLabelResult`.
- `app/providers/email/gmail_provider.py` — scope widened
  `gmail.readonly` → `gmail.modify` (narrowly used: label read/create/write
  only, no send/delete). Implements `apply_label` (creates/reuses a
  `Agent/<label>` Gmail label, attaches via `messages.modify`) and
  `get_current_label` (reads `labelIds`, reverse-maps to label name).
- `app/jobs/label_reconciliation_job.py` — new. Periodic sweep: for each
  recently-classified email, compares the DB's `relationship` against the
  provider's live `Agent/<label>`, and repairs drift by retrying the
  provider write (DB is treated as source of truth, since it reflects the
  Stage 1 decision).
- `app/jobs/__init__.py`, `app/agents/embedding/__init__.py`,
  `app/agents/memory/__init__.py` — new packages.

---

## Phase 7 — Human correction endpoint + dashboard action

**New/changed:**
- `app/api/emails.py` — rewritten. `POST /api/emails/{id}/correct-label`:
  updates the stored row → appends `label_corrections` → calls
  `apply_human_correction` (sender_memory) → re-invokes `apply_label` to
  fix the real inbox. `GET /api/emails/` drops priority filtering, filters
  by `relationship`/`department`/`confidence_tier`/`is_meeting` instead,
  and adds `open_in_inbox_url` (Gmail/Outlook deep link, provider-aware).
- `frontend/src/App.jsx` — "This is mislabeled" action (list row + detail
  modal), posts to the correction endpoint; Priority-based filter chips
  and the priority color map removed; email rows/modal no longer render
  `subject`/`summary` (never returned by the API — specs v3 §6).

---

## Phase 8 — Outlook email/calendar provider implementations

**New:**
- `app/providers/email/outlook_provider.py` — `OutlookProvider` via
  Microsoft Graph (`/me/messages`, header extraction, `categories`-based
  `apply_label`/`get_current_label` — Outlook's nearest equivalent to
  Gmail's labels, namespaced `Agent/<label>` for parity).
- `app/providers/calendar/outlook_calendar_provider.py` —
  `OutlookCalendarProvider` via Graph `/me/events`. `add_event()` only
  ever called from `POST /api/meetings/{id}/confirm`, never autonomously.
- `app/providers/email/__init__.py`, `app/providers/calendar/__init__.py` —
  factory functions (`get_email_provider`, `get_calendar_provider`)
  resolving `EmailProviderEnum`/`CalendarProviderEnum` to a concrete class.
- `app/api/auth.py` — `get_provider_access_token()`, provider-agnostic
  token extraction (`Authorization` / `X-Google-Token` / `X-Outlook-Token`).
- `app/main.py` — `get_user_profile` now branches on which token header is
  present (Google userinfo vs Microsoft Graph `/me`); new
  `GET /api/auth/outlook-config`; new `PATCH /api/user/providers`.
- `app/api/meetings.py` — `get_calendar_provider` dependency now resolves
  per-user via `UserModel.calendar_provider` instead of being Google-only.
- `frontend/src/App.jsx` — Outlook sign-in button (MS OAuth2 implicit-flow
  redirect, since no MS SDK script is loaded), Settings tab gains
  email/calendar provider selectors + org-domains input.

---

## Phase 9 — Analytics endpoints + frontend Analytics tab

**New:**
- `app/api/analytics.py` — 9 endpoints: `volume-trend`,
  `relationship-distribution`, `meeting-funnel`, `needs-review-queue`,
  `top-senders`, `trust-tier-breakdown`, `promotional-noise-ratio`,
  `label-accuracy-over-time` (the featured "is the agent improving" widget,
  driven by `label_corrections`), `reasoning-agreement-rate`,
  `suspicious-count`.
- `app/agents/models/classified_email.py` / `app/db/models.py` — added
  `reflection_agreement` column (needed to actually compute the
  agreement-rate widget rather than approximate it).
- `frontend/src/App.jsx` — new **Analytics** tab, dependency-free CSS-bar
  visualizations (`BarRow`, `StatLine` helper components), suspicious-count
  card kept visually prominent per specs v3 §8.

---

## Additional changes not itemized above

- `app/agents/orchestration/orchestrator.py` — rewritten: Stage 0
  short-circuit → `reasoning_engine` (Stage 1) → conditional
  `detect_meeting` (Stage 2, only when `is_meeting` was flagged) → persist.
  Resolves the configured email/calendar provider per user via the new
  factories. Tracks `stage0_resolved` in the run log.
- `app/agents/tools/detect_meeting.py` — dead `_MEETING_LINK_PATTERNS`
  extracted into the real `app/agents/tools/link_extractor.py`; prompt now
  includes regex-found candidate links as extraction hints.
- `app/agents/tools/write_to_data_store.py` — rewritten for the new schema;
  added `resolve_and_purge_meeting()` (content-purge on confirm/dismiss —
  nulls `meeting_title`/`meeting_datetime`/attendees/location/summary,
  stamps `resolution`/`resolved_at`/`calendar_event_id`).
- `app/jobs/thread_memory_expiry_job.py`, `meeting_purge_job.py` — new
  scheduled jobs; `meeting_purge_job` is a safety net for Pending cards
  that age out without a user decision (synchronous purge on
  confirm/dismiss already handles the normal path).
- `app/scheduler/scheduler.py` — registers all three new jobs
  (`label_reconciliation`, `thread_memory_expiry`, `meeting_purge`)
  alongside the existing per-user batch jobs.
- `app/api/batch.py`, `app/main.py` — provider-agnostic token dependency;
  `stage0_resolved` surfaced in batch-log responses; `/api/user/reset`
  clears the new memory/correction tables too.
- `requirements.txt` — added `alembic`, `psycopg2-binary`, `gunicorn`.
- Tests: removed the now-invalid `test_classify_email.py`; added
  `tests/tools/test_stage0_bulk_filter.py`,
  `tests/tools/test_confidence_engine.py`,
  `tests/tools/test_link_extractor.py`,
  `tests/agents/memory/test_sender_memory_store.py`.

---

## Deployment (new)

- `Dockerfile` — backend image (Python 3.11-slim, gunicorn+uvicorn workers,
  non-root user, runs `alembic upgrade head` via `entrypoint.sh` before boot).
- `frontend/Dockerfile` + `nginx.conf` — multi-stage frontend build, served
  via nginx with SPA fallback routing and static-asset caching.
- `docker-compose.yml` — `backend` + `frontend` + `ollama` (default local
  LLM) + optional `postgres` (via `--profile postgres`). Named volumes for
  sqlite data, Ollama model cache, and Postgres data. `./secrets/` bind-mount
  (read-only) for OAuth credential/token files — never baked into the image.
- `.env.example` — every config var used anywhere in the codebase,
  secret-free.
- `.gitignore` / `.dockerignore` — exclude `.env`, `secrets/`, any
  `*credential*.json`/`*token*.json`, `__pycache__`, `node_modules`, `dist`,
  `*.db`.
- `DEPLOYMENT.md` — first-run guide, migration notes, Postgres opt-in,
  scaling notes (stateless backend, single-scheduler-replica caveat),
  local dev instructions.
- `frontend/src/App.jsx` — `API_BASE` now reads `import.meta.env.VITE_API_BASE`
  at build time (was hardcoded to `localhost:8000`), so the same build
  process works for any deployment target.

**Verified before packaging:** `python3 -m py_compile` on every backend
file, a full `app.main` import against a scratch sqlite DB, `pytest tests/`
(31 passed), `alembic upgrade head` against a scratch DB (creates all 9
tables correctly), and `npm run build` on the frontend (clean build, no
errors).

## Known gaps / explicitly out of scope

- `app/dashboard/streamlit_app.py` (legacy Streamlit dashboard) was **not**
  updated for the v3 schema — marked deprecated in-file. The maintained
  dashboard is `frontend/` (React), per pipeline_changes.md §7. The
  Streamlit app still runs (it reads API fields via `dict.get()` with
  defaults, so it won't crash) but shows stale Priority-based UI.
- The Outlook OAuth flow uses the browser-native OAuth2 implicit flow
  rather than MSAL.js, to avoid adding a new frontend dependency — fine
  for a first deployment, but MSAL.js (with PKCE + silent refresh) is the
  better long-term choice if Outlook becomes the primary provider for most
  users.
- Reconciliation, thread-memory-expiry, and meeting-purge jobs currently
  run on a single scheduler instance (see Scaling notes in DEPLOYMENT.md);
  they are not distributed-lock-protected.
