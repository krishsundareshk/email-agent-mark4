# Email Agent

An autonomous inbox assistant that reads your Gmail, classifies every
incoming email, remembers how you treat different senders over time, and
surfaces meeting invitations for one-click calendar confirmation — without
ever touching send, delete, or reply.

This README describes what the product does and why it's built the way it
is. For local setup, see `RUNNING_LOCALLY.md`. For a phase-by-phase build
log of the v3 rewrite, see `CHANGES.md`.

---

## What it does

Point it at a Gmail inbox and it will, on a schedule or on demand:

1. **Classify every email** along two independent dimensions — *who this
   sender is to you* (Internal / Client / Vendor / Automated-System /
   Unknown-External / Suspicious / Promotional) and *what department it
   belongs to* (HR / Finance / IT / Legal / Operations / General) — then
   apply that as a real Gmail label.
2. **Detect meeting invitations**, extract title, date/time, duration,
   attendees, and video-call link, and hold them as pending cards you can
   accept (adds to your Google Calendar) or decline — never added
   automatically.
3. **Learn per-sender behavior over time.** If you keep re-labeling a
   sender's mail the same way, the agent's confidence in that label rises;
   if you correct it, that correction is remembered and weighted into
   future decisions for that sender specifically.
4. **Flag its own uncertainty** rather than guessing silently — emails the
   pipeline isn't confident about land in a "needs review" queue instead of
   an auto-applied label.

It deliberately does **not** send, reply to, forward, or delete email, and
never adds a meeting to your calendar without an explicit click.

---

## Why it's built this way

### Relationship, not Priority

The original design classified email urgency ("Priority: High/Medium/Low").
The current version (v3) replaced that entirely with *relationship*
classification — who the sender is to you — on the reasoning that urgency is
subjective and drifts, while sender relationship is a much more stable,
reusable signal: once the agent knows a sender is a known vendor or an
internal teammate, that fact keeps paying off on every future email from
them, whereas a priority score has to be re-guessed every time.

### Confidence tiers instead of a single float threshold

Rather than one confidence score with a cutoff, classification confidence is
computed from five separate signals — self-reported model certainty,
whether a second reflection pass agreed with the first, whether the
decision matches the sender's memory, how close the top two candidate
labels were, and whether Stage-0 heuristics corroborated the LLM's read —
and combined into one of three tiers: `auto-applied`, `needs-review`, or
`unclassified`. Any single suspicious signal (phishing/spoofing cues) always
forces `needs-review`, regardless of how confident everything else looks —
uncertainty is surfaced, never averaged away.

### Two-pass reasoning (Observe → Reason → Reflect → Finalize)

Each email goes through the reasoning engine twice: a first pass produces a
tentative relationship/department/is-meeting call, then a second pass is
explicitly asked to check that tentative call against what's known about the
sender and flag disagreement (`confirmed` / `revised` / `reversed`). This
catches cases where the model's first read contradicts established sender
history, which a single-pass classifier can't self-correct.

### Meeting detection is a second, stricter gate — not the same call

Whether an email even *might* be a meeting is decided as part of the Stage-1
classification pass (cheap, folded into the same call). Only emails that
clear that gate go to a second, dedicated extraction pass
(`detect_meeting.py`) that pulls out the structured date/time/link/attendee
fields needed to actually build a calendar event. This two-gate design
exists to avoid running full structured extraction on every single email —
but it does mean a Stage-1 miss (the first pass deciding an email isn't a
meeting) silently skips extraction entirely, which is a known sharp edge on
smaller local models — see **Known limitations** below.

### Deterministic filtering before any LLM call

A zero-LLM Stage 0 filter runs first, using only RFC email headers
(`List-Unsubscribe`, `Precedence`) to catch obvious bulk/promotional mail.
This both saves LLM calls on the highest-volume, lowest-value category of
email and keeps Promotional classification perfectly consistent, since it's
not subject to model variance at all.

### Per-sender and per-thread memory, with a combined-distribution fallback

Every classified email updates a running label distribution for that
sender. New/unfamiliar senders fall back to a *global* label centroid
(a semantic-similarity prior over label embeddings) rather than a blind
guess, so the very first email from someone new still gets a reasonably
informed classification instead of a coin flip.

### Content never persisted — only structured judgments are

Subject and body text are held in memory only for the duration of
processing a given email and are never written to the database — what
persists is the structured output (relationship, department, confidence
tier, meeting metadata) and, for meetings, an eventual content-free audit
stub once a meeting is resolved (added/dismissed). This was a deliberate
scope decision to keep the stored data minimal.

### Two providers on each side, swappable independently

Email (Gmail / Outlook) and Calendar (Google Calendar / Outlook Calendar)
are each behind a small provider interface (`app/providers/email/base.py`,
`app/providers/calendar/base.py`), and LLM reasoning vs. embeddings are
configured as separate provider knobs (`LLM_PROVIDER` / `EMBEDDING_PROVIDER`)
so swapping the reasoning model doesn't silently invalidate the embedding
space that sender-memory centroids were built from.

---

## Architecture at a glance

```
Gmail / Outlook  ──►  Stage 0: deterministic bulk-mail filter (no LLM)
                            │  (Promotional short-circuits here)
                            ▼
                    Stage 1: reasoning_engine.py
                    Observe → Reason → Reflect → Finalize
                    (relationship, department, is_meeting,
                     confidence signals, apply Gmail label,
                     update sender/thread memory)
                            │
                            │  only if is_meeting == true
                            ▼
                    Stage 2: detect_meeting.py
                    (title, datetime+offset, duration,
                     attendees, video-call link)
                            │
                            ▼
                    Stage 3: confidence_engine.py
                    (5-signal rubric → auto-applied /
                     needs-review / unclassified)
                            │
                            ▼
                    SQLite / Postgres  ◄──►  FastAPI backend
                                                    │
                                                    ▼
                                        React dashboard (frontend/)
                                        — email list, Meeting RSVPs
                                        (confirm/dismiss), Analytics
```

**Backend:** FastAPI (`app/main.py`), SQLAlchemy + Alembic, APScheduler for
the optional background batch/reconciliation jobs.

**Frontend:** React + Vite (`frontend/src/App.jsx`) — single-page dashboard
with three tabs: email list (relationship/department/meeting badges),
Meeting RSVPs (accept/decline pending meeting cards), and Analytics (volume
trend, relationship distribution, meeting funnel, needs-review queue).

**LLM layer:** provider-agnostic client (`app/agents/tools/llm_client.py`)
supporting Ollama (local), OpenAI, or Gemini for both reasoning and
embeddings independently.

---

## Authentication

Two independent Google OAuth paths exist, solving different problems:

- **Interactive login** (what the dashboard uses) — the browser handles the
  OAuth token exchange directly via Google Identity Services and sends a
  short-lived access token to the backend per request. Nothing is written
  to disk. This is the path this deployment runs on.
- **Unattended access** — an `InstalledAppFlow`-based flow for the
  background scheduler to act without a live browser session, used only if
  `ENABLE_SCHEDULER=true`. Not configured in this deployment.

---

## Known limitations

- **Stage-1 meeting gate can silently miss real invites**, especially on
  smaller local models — if the first classification pass decides an email
  isn't a meeting, the second, stricter extraction pass never runs, and no
  meeting card is created at all (nothing to accept/decline). A
  regex-matched video-call link forcing Stage 2 to run regardless of Stage
  1's call would close this gap but isn't implemented yet.
- **Extraction quality depends on the LLM provider.** Local models
  (Ollama) are meaningfully less reliable at structured extraction —
  meeting datetimes, timezones, links — than GPT-class models. A previous
  timezone-handling bug (stated timezones being silently dropped and
  mistaken for UTC) has been fixed in the prompt, but correct timezone
  extraction still depends on the model correctly reading the source text.
- **No message deletion, send, or reply capability**, by design — the
  agent only reads, labels, and reads calendar-relevant metadata.
- **Single-replica assumption on SQLite.** Postgres is supported for
  multi-replica setups but this deployment runs SQLite, which is
  single-writer.

---

## Repo layout

```
app/
  agents/
    orchestration/   → orchestrator.py, wires the pipeline stages together
    tools/            → stage0_bulk_filter, reasoning_engine, detect_meeting,
                         confidence_engine, link_extractor, llm_client
    memory/            → sender_memory_store, thread_memory_store
    embedding/         → embedding_adapter
    models/            → Pydantic models + enums
  api/                 → FastAPI routers (auth, emails, meetings, batch, analytics)
  providers/
    email/             → GmailProvider, OutlookProvider
    calendar/           → GoogleCalendarProvider, OutlookCalendarProvider
  db/                  → SQLAlchemy models, session, seeding
  scheduler/           → APScheduler batch/reconciliation jobs (optional)
  main.py              → FastAPI app entrypoint
frontend/               → React + Vite dashboard
alembic/                → DB migrations
tests/                  → pytest suite
```
