"""
AgentFlow FastAPI Server — Phase 3
--------------------------------
Exposes the pipeline as an HTTP API so n8n can trigger it via webhook.

Endpoints:
  POST /run        — trigger a full pipeline run
  GET  /status     — check server health
  GET  /runs       — list recent pipeline runs from DB
  POST /approve    — approve/reject a specific outreach message (feedback loop)

Run:
  uvicorn api.server:app --host 0.0.0.0 --port 8001 --reload

n8n webhook triggers this server at: http://localhost:8001/run
"""

import os
import json
import psycopg2
from typing import Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from loguru import logger

from llm_config import llm_manager
import scheduler

load_dotenv()

app = FastAPI(
    title="AgentFlow API",
    description="Sovereign Multi-Agent Growth & Governance Engine",
    version="1.0.0",
)

# Allow n8n (localhost:5678) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the HTML dashboard at /dashboard
_static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

@app.get("/dashboard", include_in_schema=False)
def serve_dashboard():
    """Open the Growth Engine dashboard in your browser."""
    return FileResponse(os.path.join(_static_dir, "dashboard.html"))

@app.get("/architecture", include_in_schema=False)
def serve_architecture():
    """Open the system architecture diagram in your browser."""
    return FileResponse(os.path.join(_static_dir, "architecture.html"))


@app.on_event("startup")
def _startup():
    """Cloud bootstrap: create tables if the DB is fresh, then restore state.

    On Neon/Supabase/Render the database starts EMPTY, so we must apply
    database/init.sql before anything queries it. Safe to run every boot.
    """
    try:
        from migrations.run import migrate
        migrate()
    except Exception as e:
        logger.warning(f"Schema migration skipped: {e}")

    """Restore the last-run snapshot from Postgres after a restart.

    Without this, every Render deploy / crash would blank the dashboard's
    live-output panel even though the DB still holds all the leads.
    """
    try:
        import crew  # lazy — heavy (crewai) import
        crew.hydrate_last_run_from_db()
    except Exception as e:
        logger.warning(f"LAST_RUN hydration skipped: {e}")


# ─── Request / Response Models ────────────────────────────────────────────────

class PipelineRequest(BaseModel):
    industry:  str = "SaaS"
    job_title: str = "Head of Growth"
    location:  str = "United States"
    limit:     int = 5
    trigger:   str = "n8n_webhook"


class ApprovalRequest(BaseModel):
    outreach_id: int
    approved:    bool
    reason:      Optional[str] = ""


# ─── DB Helper ────────────────────────────────────────────────────────────────

def get_db():
    # Cloud-ready: honours DATABASE_URL (Neon/Supabase) or POSTGRES_* vars,
    # and applies sslmode=require for remote hosts. See db_config.py.
    from db_config import get_conn
    return get_conn()


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/status")
def health_check():
    """n8n uses this to verify the server is alive before triggering a run."""
    return {
        "status":    "ok",
        "service":   "AgentFlow",
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/run")
async def run_pipeline(request: PipelineRequest, background_tasks: BackgroundTasks):
    """
    Triggers a full Scout → Analyst → Writer → Security Guard pipeline run.
    Called by n8n via webhook or timer.
    Returns immediately with run metadata; pipeline runs in background.
    """
    logger.info(f"Pipeline triggered via API | trigger={request.trigger}")

    # Import here to avoid circular imports
    from crew import run_pipeline as execute_pipeline

    # Run in background so n8n doesn't time out waiting
    background_tasks.add_task(
        execute_pipeline,
        industry=request.industry,
        job_title=request.job_title,
        location=request.location,
        limit=request.limit,
        trigger=request.trigger,
    )

    return {
        "status":    "started",
        "message":   f"Pipeline triggered for {request.industry} / {request.job_title}",
        "trigger":   request.trigger,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/runs")
def list_runs(limit: int = 10):
    """Returns recent pipeline run history from the database."""
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT id, status, leads_found, leads_processed,
                   trigger_source, started_at, completed_at
            FROM pipeline_runs
            ORDER BY started_at DESC
            LIMIT %s
        """, (limit,))

        columns = ["id", "status", "leads_found", "leads_processed",
                   "trigger_source", "started_at", "completed_at"]
        runs = [dict(zip(columns, row)) for row in cur.fetchall()]

        # Convert datetimes to strings
        for run in runs:
            for key in ["started_at", "completed_at"]:
                if run[key]:
                    run[key] = run[key].isoformat()

        cur.close()
        conn.close()
        return {"runs": runs, "total": len(runs)}

    except Exception as e:
        # Production-friendly: DB may be down/sleeping. Return empty, don't 500.
        logger.warning(f"/runs unavailable (DB down?): {e}")
        return {"runs": [], "total": 0, "db_available": False}


@app.get("/leads")
def list_leads(status: str = None, limit: int = 20):
    """Returns leads with their outreach status and security scores."""
    try:
        conn = get_db()
        cur  = conn.cursor()

        query = """
            SELECT l.full_name, l.job_title, l.company, l.industry, l.location,
                   o.subject_line, o.message, o.status, o.security_score,
                   o.rejection_reason, o.created_at
            FROM leads l
            JOIN outreach o ON l.id = o.lead_id
        """
        params = []
        if status:
            query += " WHERE o.status = %s"
            params.append(status)
        query += " ORDER BY o.created_at DESC LIMIT %s"
        params.append(limit)

        cur.execute(query, params)
        columns = ["full_name", "job_title", "company", "industry", "location",
                   "subject_line", "message", "status", "security_score",
                   "rejection_reason", "created_at"]
        leads = [dict(zip(columns, row)) for row in cur.fetchall()]

        for lead in leads:
            if lead.get("created_at"):
                lead["created_at"] = lead["created_at"].isoformat()

        cur.close()
        conn.close()
        return {"leads": leads, "total": len(leads)}

    except Exception as e:
        # Production-friendly: DB may be down/sleeping. Return empty, don't 500.
        logger.warning(f"/leads unavailable (DB down?): {e}")
        return {"leads": [], "total": 0, "db_available": False}


@app.post("/approve")
def approve_outreach(request: ApprovalRequest):
    """
    Human approve/reject endpoint — called from the Phase 4 dashboard.
    Updates outreach status and feeds rejection reason into the feedback loop.
    """
    try:
        from feedback import FeedbackLoop
        loop = FeedbackLoop()
        loop.log_approval_decision(
            outreach_id=request.outreach_id,
            approved=request.approved,
            reason=request.reason,
        )

        return {
            "status":      "updated",
            "outreach_id": request.outreach_id,
            "approved":    request.approved,
            "timestamp":   datetime.now().isoformat(),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Content Generation (Phase 5 — AI content tooling) ────────────────────────

class ContentRequest(BaseModel):
    topic:    str
    platform: str = "email"       # email | twitter | linkedin | blog
    tone:     str = "conversational"
    variants: int = 2


@app.post("/content")
def generate_content(request: ContentRequest):
    """
    Uses the active LLM (Claude/GPT/Gemini/OpenRouter/Ollama via fallback chain)
    to generate marketing content variants. Demonstrates AI content tooling.
    """
    logger.info(f"Content generation | topic={request.topic!r} | platform={request.platform}")

    prompt = (
        f"Write {request.variants} short {request.platform} content variant(s) about: "
        f"{request.topic}. Tone: {request.tone}. Keep each under 60 words, "
        f"engaging, no hashtags spam. Return as a JSON array of strings."
    )
    try:
        raw = llm_manager.complete(
            prompt,
            system="You are a concise B2B content writer. Return only the JSON array.",
            max_tokens=600,
            temperature=0.7,
        )
        import json, re
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        variants = json.loads(m.group()) if m else [raw]
        return {
            "status":   "ok",
            "provider": llm_manager.get_crewai_llm(),
            "topic":    request.topic,
            "platform": request.platform,
            "content":  variants,
        }
    except Exception as e:
        logger.error(f"Content generation failed: {e}")
        raise HTTPException(status_code=503, detail=f"LLM unavailable: {e}")


@app.get("/providers")
def list_providers():
    """Shows the configured LLM provider chain + which one is active. Handy for ops/debug."""
    active = llm_manager.resolve()
    return {
        "chain":   llm_manager.providers,
        "active":  active.spec.name if active else None,
        "model":   active.model if active else None,
        "timeout": llm_manager.timeout,
    }


# ─── Scheduling (Design A — n8n polls /jobs/due) ──────────────────────────────

class ScheduleRequest(BaseModel):
    industry:  str = "SaaS"
    job_title: str = "Head of Growth"
    location:  str = "United States"
    limit:     int = 5
    schedule_kind: str = "once"             # once | daily
    run_in_minutes: Optional[int] = None    # relative (e.g. 30 → "in 30 min")
    time_of_day:    Optional[str] = None    # "08:00" for a specific/daily time


class TriggerAck(BaseModel):
    failed: bool = False


def _compute_run_at(req: ScheduleRequest):
    """Turn the user's schedule input into an absolute next-run datetime."""
    now = datetime.now()
    if req.run_in_minutes is not None:
        return now + timedelta(minutes=max(0, req.run_in_minutes)), "once", None
    if req.time_of_day:
        try:
            hh, mm = [int(x) for x in req.time_of_day.split(":")[:2]]
        except Exception:
            hh, mm = 9, 0
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        kind = "daily" if req.schedule_kind == "daily" else "once"
        return candidate, kind, f"{hh:02d}:{mm:02d}"
    # default: fire 10 minutes from now
    return now + timedelta(minutes=10), "once", None


@app.post("/schedule")
def schedule_run(request: ScheduleRequest):
    """Save a scheduled pipeline run. n8n polls /jobs/due and triggers it later."""
    run_at, kind, tod = _compute_run_at(request)
    job = scheduler.create_job(
        industry=request.industry, job_title=request.job_title,
        location=request.location, limit=request.limit,
        schedule_kind=kind, run_at=run_at, time_of_day=tod,
    )
    logger.info(f"Job scheduled: kind={kind} run_at={run_at} store={job.get('store')}")
    return {
        "status":  "scheduled",
        "job":     job,
        "message": f"Run scheduled for {run_at:%Y-%m-%d %H:%M} ({kind})",
    }


@app.get("/jobs")
def get_jobs(limit: int = 50):
    """List all scheduled jobs (for the dashboard schedule panel)."""
    jobs = scheduler.list_jobs(limit=limit)
    return {"jobs": jobs, "total": len(jobs)}


@app.get("/jobs/due")
def get_due_jobs():
    """
    Called by n8n every few minutes (Design A). Returns any pending jobs whose
    fire time has arrived so n8n can POST /run for each and then acknowledge.
    """
    jobs = scheduler.due_jobs()
    return {"jobs": jobs, "total": len(jobs), "checked_at": datetime.now().isoformat()}


@app.post("/jobs/{job_id}/triggered")
def acknowledge_job(job_id: int, ack: TriggerAck):
    """n8n calls this after firing a job. Rolls daily jobs to their next run."""
    ok = scheduler.mark_triggered(job_id, failed=ack.failed)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return {"status": "acknowledged", "job_id": job_id, "failed": ack.failed}


@app.post("/scheduler/tick")
async def scheduler_tick(background_tasks: BackgroundTasks):
    """
    Self-contained scheduler tick — the n8n scheduled branch, without n8n.

    Locally, n8n runs this exact sequence every 5 minutes:
      GET /jobs/due → POST /run per due job → POST /jobs/{id}/triggered.
    Hosting n8n is OPTIONAL: point any free external cron (e.g. cron-job.org,
    no credit card) at this endpoint every 5 minutes and user-set times still
    re-fire the pipeline in the cloud exactly as they do locally. The n8n
    workflow keeps working too — whichever fires first marks the job done,
    so a job can never double-fire.
    """
    jobs = scheduler.due_jobs()
    fired = []
    for job in jobs:
        from crew import run_pipeline as execute_pipeline
        background_tasks.add_task(
            execute_pipeline,
            industry=job.get("industry"),
            job_title=job.get("job_title"),
            location=job.get("location"),
            limit=int(job.get("limit") or 5),
            trigger="schedule",
        )
        # Acknowledge immediately (same semantics as n8n: POST /run only
        # schedules the background task, so a failure here isn't silent —
        # daily jobs roll forward and 'once' jobs go to done either way).
        scheduler.mark_triggered(job["id"])
        fired.append(job["id"])
        logger.info(f"Scheduler tick fired job {job['id']} "
                    f"({job.get('industry')}/{job.get('job_title')})")

    return {
        "status":     "ok",
        "due":        len(jobs),
        "fired":      fired,
        "checked_at": datetime.now().isoformat(),
    }


@app.get("/last-run")
def get_last_run_state():
    """
    Latest pipeline output from memory (works even without a DB). The dashboard
    polls this after clicking 'Run' to show real agent output + security scores.
    """
    import crew  # lazy — avoids circular import at startup
    return {"run": crew.get_last_run()}
