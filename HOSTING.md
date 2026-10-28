# Hosting Guide — AgentFlow (free tier, **no credit/debit card**)

This project runs identically on your laptop and in the cloud. Everything that
normally breaks after hosting has been handled in code — see
[What was made hosting-ready](#what-was-made-hosting-ready).

---

## Recommended stack (0 ₹ / 0 $, email sign-up only)

| Layer  | Platform            | Why                                                        |
| ------ | ------------------- | ---------------------------------------------------------- |
| API + dashboard | **Render** (free web service) | Email sign-up, **no card**, `render.yaml` blueprint included, serves `/dashboard` over HTTPS |
| Database | **Neon** (free tier) | Email sign-up, **no card**, hands you one `DATABASE_URL`, SSL built in. *Supabase free works too (same idea).* |
| LLM    | **OpenRouter** (free models) + **Groq** fallback | Email sign-up, **no card**. `llm_config.py` fails over automatically — no Ollama in the cloud |
| Scheduler | **cron-job.org** (free) *or* n8n | Email sign-up, **no card** — pings `/scheduler/tick` every 5 min (see below) |

> Platforms that **do** need a card (avoid for this): Railway, Fly.io, Oracle
> Cloud, Google Cloud, AWS. Hugging Face Spaces works without a card but sleeps
> aggressively and has no durable Postgres — Render + Neon is the cleaner fit.

---

## Deploy in 10 minutes

### 1. Database — Neon (2 min)

1. Sign up at <https://neon.tech> (email only).
2. Create a project → copy the **connection string** (looks like
   `postgresql://user:pass@ep-xxx.aws.neon.tech/neondb?sslmode=require`).
3. That's it. **No tables to create** — the app runs `database/init.sql`
   automatically on startup (`migrations/run.py`), so an empty DB is fine.

### 2. API + dashboard — Render (5 min)

1. Push this repo to GitHub.
2. Sign up at <https://render.com> (email only, **no card**).
3. **New → Blueprint** → pick the repo. Render reads `render.yaml` and creates
   the `agentflow-api` web service (Python 3.12, health check on `/status`).
4. In the service → **Environment**, add:

   | Key | Value |
   | --- | --- |
   | `DATABASE_URL` | the Neon connection string (mark as Secret) |
   | `OPENROUTER_API_KEY` | from openrouter.ai (free credits / free models) |
   | `GROQ_API_KEY` | from console.groq.com (fast free fallback) |
   | `HUGGINGFACEHUB_API_TOKEN` | optional backup |

   `LLM_PROVIDERS=openrouter,groq,huggingface,ollama` and `PYTHON_VERSION=3.12.0`
   are already set by `render.yaml`.

5. Deploy. When it's green you get `https://agentflow-xxxx.onrender.com` —
   open `/dashboard` on it. That's the link to share.

**That's the whole deploy.** Startup does, in order:

```
migrate()                      → creates tables if the DB is fresh
hydrate_last_run_from_db()     → dashboard shows the last real run, not blanks
Application startup complete   → /status 200 (Render health check passes)
```

### 3. Scheduled runs — pick one (2 min)

User-set times are stored in Postgres (`POST /schedule`), so they survive
restarts on any platform. Something must then *check* for due jobs every
5 minutes — two no-card options:

**Option A — free cron (simplest, no n8n hosting needed)**

Point <https://cron-job.org> (email sign-up) at:

```
POST https://<your-app>.onrender.com/scheduler/tick   (every 5 minutes)
```

`/scheduler/tick` does exactly what the n8n branch does: check due jobs →
fire each pipeline run → mark it triggered (daily jobs roll to tomorrow).
Whichever of n8n / cron fires first marks the job done, so a job can never
double-fire even if you run both.

**Option B — your local n8n driving the hosted API**

Keep `n8n` running in Docker on your machine and set one env var so the
workflow points at the cloud instead of localhost:

```bash
AGENTFLOW_API_URL=https://<your-app>.onrender.com
```

Every URL in `n8n/agentflow_workflow.json` already reads that variable with a
local fallback, so the same workflow file works before and after hosting.
Re-import & activate the workflow if you changed it
(`docker exec agentflow_n8n ...` — see README "n8n workflow" section).

---

## Cold starts (the only free-tier gotcha)

Render's free service **sleeps after ~15 min without traffic**; the first
request then takes ~30–50 s while it wakes up. Mitigations already built in:

- The 5-minute scheduler poll (`/scheduler/tick` or n8n) keeps it awake
  during the day.
- A sleeping DB or API never crashes the app: DB calls use
  `connect_timeout=10` and endpoints degrade to empty-with-warning instead
  of 500, then recover on the next poll.
- Health check on `/status` is DB-independent, so a sleeping Neon never
  causes a Render restart loop.

If the reviewer might hit a cold link, ping your `/status` once a minute with
any free uptime monitor (e.g. UptimeRobot free tier, no card) — that also
keeps it awake 24/7.

---

## What was made hosting-ready

| Problem (would break after hosting) | Fix |
| --- | --- |
| Dashboard called `http://localhost:8001` → would hit the *reviewer's* machine | `API = location.origin` when served over http(s) (`static/dashboard.html`) |
| 5 modules each built their own DB config, none supported SSL | single `db_config.py` used by crew / scheduler / feedback / server / dashboard |
| Neon/Supabase `DATABASE_URL` unsupported, `sslmode=require` never set | `db_config.py` parses the URL, normalises `postgres://`, applies `require` for remote hosts, `prefer` for localhost (`POSTGRES_SSLMODE` overrides) |
| Fresh cloud DB has **no tables** → every query fails | `migrations/run.py` applies `database/init.sql` at FastAPI startup (idempotent) |
| Restarts blanked the live-output panel | startup hydration restores `LAST_RUN` from Postgres |
| n8n workflow hardcoded `host.docker.internal:8001` | all 7 URLs read `$env.AGENTFLOW_API_URL` with local fallback |
| Scheduled runs depended on hosting n8n | new `POST /scheduler/tick` endpoint = the n8n branch, callable by any free cron |
| Cloud build could time out on unused heavy deps | `requirements.txt` trimmed (`opencv`, `sqlalchemy`, `ollama`, `streamlit`, `plotly` removed — none imported by the served app; legacy extras in `requirements-legacy.txt`) |
| Wrong provider chain in `render.yaml` (`anthropic,openai,gemini` without keys) | corrected to the verified `openrouter,groq,huggingface,ollama` |

## Verify after deploy

```bash
curl https://<your-app>.onrender.com/status          # {"status":"ok",...}
curl https://<your-app>.onrender.com/leads?limit=5   # leads from Neon
curl https://<your-app>.onrender.com/scheduler/tick  # {"status":"ok","due":0,...}
open  https://<your-app>.onrender.com/dashboard      # full UI, mobile-ready
```

Then click **Run now** in the dashboard — the live row should show
`● Running` → `Done · fired 1×`, and the Download page updates on its own
(30 s auto-sync + invalidation on run completion).
