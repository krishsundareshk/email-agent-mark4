# Running Locally (no Docker)

This replaces the old Docker-based `DEPLOYMENT.md`. Docker files
(`Dockerfile`, `docker-compose.yml`, `nginx.conf`, `entrypoint.sh`,
`frontend/Dockerfile`) have been removed — this is a plain
Python + Node setup.

Auth is configured for **Path A only**: interactive Google sign-in from the
dashboard (`frontend/src/App.jsx`, Google Identity Services). The backend's
other auth path — the unattended `InstalledAppFlow` used by the scheduler for
background batch jobs — is intentionally disabled (`ENABLE_SCHEDULER=false`
in `.env`), so there's no need for `GOOGLE_CREDENTIALS_PATH` /
`GOOGLE_TOKEN_PATH` or a `secrets/` folder in this setup.

## 1. Backend

```bash
pip install -r requirements.txt --break-system-packages
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Runs on `http://localhost:8000`. `GET /health` should return
`{"status":"ok", ...}`.

## 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Runs on `http://localhost:5173` and talks to the backend at
`VITE_API_BASE` (set in `.env`, defaults to `http://localhost:8000`).

## 3. Google OAuth setup (Path A)

1. In Google Cloud Console, enable the **Gmail API** and **Google Calendar
   API** for your project.
2. Configure the OAuth consent screen, add scopes `gmail.modify`, `calendar`,
   `userinfo.email`, `userinfo.profile`, and add your own Google account as a
   test user while the app is in "Testing" status.
3. Create an OAuth Client ID of type **Web application**, with
   `http://localhost:5173` as an authorized JavaScript origin.
4. Copy the Client ID into `.env` as `GOOGLE_CLIENT_ID`.
5. Restart the backend so it picks up the new value (it's served to the
   frontend via `GET /api/auth/config`).

Once that's set, open `http://localhost:5173` and use the Google sign-in
button — the browser handles the OAuth token exchange directly; nothing is
written to disk on the backend.

## 4. LLM provider — Ollama

`LLM_PROVIDER=ollama` and `EMBEDDING_PROVIDER=ollama` in `.env`, pointing at
`http://localhost:11434`. Install and run Ollama yourself (no Docker):

```bash
curl -fsSL https://ollama.com/install.sh | sh   # macOS: brew install ollama
ollama serve                                     # leave running in its own terminal
ollama pull qwen3:8b
ollama pull nomic-embed-text
```

Verify it's reachable before starting the backend:
```bash
curl http://localhost:11434/api/tags
```

## 5. What's disabled in this setup

- **Scheduler** (`ENABLE_SCHEDULER=false`) — no background batch runs, label
  reconciliation, memory expiry, or meeting purge. Flip to `true` and set up
  `GOOGLE_CREDENTIALS_PATH`/`GOOGLE_TOKEN_PATH` (Path B) if you want those.
- **Postgres / multi-replica notes** from the old deployment doc no longer
  apply — this is a single local sqlite file at `./data/email_agent.db`.
