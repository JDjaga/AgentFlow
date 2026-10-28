"""
AgentFlow — Sovereign Multi-Agent Growth & Governance Engine
==========================================================
Main entry point — Phase 1 + Phase 2.

Pipeline:
    1. Scout    → fetches leads (mock/apollo/hunter)
    2. Analyst  → enriches each lead with OCR + reasoning
    3. Writer   → drafts personalised outreach
    4. Guard    → Security Score check (PII / Injection / Hallucination / Tone)
    5. Database → approved leads saved to PostgreSQL with full security log

Run:
    python crew.py

Environment:
    Copy .env.example to .env and configure before running.
    Make sure Docker is running: docker-compose up -d
"""

import os
import json
import threading
import psycopg2
from typing import Optional
from datetime import datetime
from dotenv import load_dotenv
from crewai import Crew, Process
from rich.console import Console
from rich.panel import Panel
from loguru import logger

from agents   import build_scout, build_analyst, build_writer
from tasks    import build_scout_task, build_analyst_task, build_writer_task
from security import SecurityGuard
from feedback import FeedbackLoop

load_dotenv()
console = Console()

# ─── Last-run store (in-memory) ───────────────────────────────────────────────
# Lets the dashboard/API show the most recent pipeline output even when the DB
# is unavailable (local demo / Render free-tier cold start). On a restart this
# resets to idle — the DB remains the source of truth when connected.
_last_lock = threading.Lock()
LAST_RUN = {
    "status":      "idle",   # idle | running | completed | failed
    "params":      {},
    "leads":       [],       # secured leads from the last run
    "summary":     {},       # {total, approved, rejected, avg_security_score}
    "trigger":     "",
    "started_at":  None,
    "completed_at": None,
    "error":       "",
}


def _set_last_run(**kwargs):
    with _last_lock:
        LAST_RUN.update(kwargs)


def get_last_run() -> dict:
    with _last_lock:
        return json.loads(json.dumps(LAST_RUN))  # deep copy (safe for API)


def hydrate_last_run_from_db() -> bool:
    """Restore LAST_RUN from PostgreSQL after a restart (Render deploys, crashes).

    The in-memory snapshot dies with the process, but the DB keeps every run's
    leads. We rebuild the most recent *completed* run so the dashboard isn't
    blank after the server comes back up.
    """
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT id, status, trigger_source, started_at, completed_at
            FROM pipeline_runs
            WHERE status = 'completed'
            ORDER BY completed_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close(); return False
        run_id, status, trigger, started_at, completed_at = row

        # Rebuild the leads this run produced (most recent N outreach rows).
        cur.execute("""
            SELECT l.full_name, l.job_title, l.company, l.industry, l.location,
                   l.email, l.linkedin_url, l.source_url, l.source,
                   l.raw_data->>'analyst_notes' AS analyst_notes,
                   o.subject_line, o.message, o.security_score, o.security_log,
                   o.status, o.rejection_reason
            FROM outreach o
            JOIN leads l ON l.id = o.lead_id
            ORDER BY o.created_at DESC
            LIMIT 50
        """)
        cols = ["full_name", "job_title", "company", "industry", "location",
                "email", "linkedin_url", "source_url", "source", "analyst_notes",
                "subject_line", "message", "security_score", "security_log",
                "status", "rejection_reason"]
        leads = []
        for r in cur.fetchall():
            item = dict(zip(cols, r))
            log = item.get("security_log")
            if isinstance(log, str):
                try: item["security_log"] = json.loads(log)
                except Exception: pass
            leads.append(item)
        cur.close(); conn.close()

        if not leads:
            return False

        approved   = [x for x in leads if x.get("status") == "approved"]
        scores     = [int(x.get("security_score") or 0) for x in leads]
        avg        = round(sum(scores) / len(scores)) if scores else 0
        _set_last_run(
            status="completed",
            trigger=trigger or "dashboard",
            started_at=str(started_at) if started_at else None,
            completed_at=str(completed_at) if completed_at else None,
            leads=leads,
            summary={"total": len(leads), "approved": len(approved),
                     "rejected": len(leads) - len(approved),
                     "avg_security_score": avg},
            error="",
        )
        logger.info(f"Hydrated LAST_RUN from DB — run #{run_id}, {len(leads)} leads")
        return True
    except Exception as e:
        logger.warning(f"Could not hydrate LAST_RUN from DB: {e}")
        return False


# ─── Database ─────────────────────────────────────────────────────────────────

def get_db_connection():
    # Cloud-ready: honours DATABASE_URL (Neon/Supabase) or POSTGRES_* vars,
    # and applies sslmode=require for remote hosts. See db_config.py.
    from db_config import get_conn
    return get_conn()


def save_results(leads: list, run_id: int):
    """Persist full pipeline output (including security scores) to PostgreSQL."""
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        for item in leads:
            # 1. Insert lead
            cur.execute("""
                INSERT INTO leads (full_name, job_title, company, industry, location,
                                   email, linkedin_url, source_url, source, raw_data)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                item.get("full_name"), item.get("job_title"), item.get("company"),
                item.get("industry"),  item.get("location"),  item.get("email"),
                item.get("linkedin_url"), item.get("source_url"),
                item.get("source", "mock"), json.dumps(item),
            ))
            lead_id = cur.fetchone()[0]

            # 2. Insert analysis
            cur.execute("""
                INSERT INTO analyses (lead_id, analyst_notes)
                VALUES (%s, %s)
                RETURNING id
            """, (lead_id, item.get("analyst_notes", "")))
            analysis_id = cur.fetchone()[0]

            # 3. Insert outreach with security score and log (Phase 2)
            cur.execute("""
                INSERT INTO outreach
                    (lead_id, analysis_id, message, subject_line,
                     status, security_score, security_log, rejection_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                lead_id,
                analysis_id,
                item.get("message", ""),
                item.get("subject_line", ""),
                item.get("status", "pending"),
                item.get("security_score", 0),
                json.dumps(item.get("security_log", {})),
                item.get("rejection_reason", ""),
            ))

        # 4. Mark pipeline run complete
        cur.execute("""
            UPDATE pipeline_runs
            SET status='completed', leads_processed=%s, completed_at=%s
            WHERE id=%s
        """, (len(leads), datetime.now(), run_id))

        conn.commit()
        cur.close()
        conn.close()
        logger.success(f"Saved {len(leads)} leads to database")

    except Exception as e:
        logger.error(f"Database save failed: {e}")
        logger.warning("Results printed to terminal only.")


def start_pipeline_run(trigger: str = "manual") -> Optional[int]:
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO pipeline_runs (trigger_source) VALUES (%s) RETURNING id",
            (trigger,)
        )
        run_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return run_id
    except Exception as e:
        logger.warning(f"Could not log pipeline run to DB ({e}). Continuing without DB.")
        return None


# ─── Main Pipeline ────────────────────────────────────────────────────────────

def run_pipeline(
    industry:  str = None,
    job_title: str = None,
    location:  str = None,
    limit:     int = 5,
    trigger:   str = "manual",
):
    industry  = industry  or os.getenv("TARGET_INDUSTRY",  "SaaS")
    job_title = job_title or os.getenv("TARGET_JOB_TITLE", "Head of Growth")
    location  = location  or os.getenv("TARGET_LOCATION",  "United States")

    console.print(Panel.fit(
        f"[bold cyan]AgentFlow — Phase 1 + 2 Pipeline[/bold cyan]\n"
        f"Industry: [yellow]{industry}[/yellow]  |  "
        f"Title: [yellow]{job_title}[/yellow]  |  "
        f"Location: [yellow]{location}[/yellow]  |  "
        f"Leads: [yellow]{limit}[/yellow]",
        border_style="cyan"
    ))

    run_id = start_pipeline_run(trigger)

    _set_last_run(
        status="running",
        params={"industry": industry, "job_title": job_title,
                "location": location, "limit": limit},
        trigger=trigger,
        started_at=datetime.now().isoformat(),
        completed_at=None, error="", leads=[], summary={},
    )

    # ── Phase 3: Load feedback from previous rejections ───────────────────────
    feedback_loop    = FeedbackLoop()
    feedback_context = feedback_loop.get_feedback_context()
    if feedback_context:
        console.print("[bold magenta]Feedback loop active — Writer is learning from past rejections.[/bold magenta]")

    # ── Phase 1: Agents ───────────────────────────────────────────────────────
    scout   = build_scout()
    analyst = build_analyst()
    writer  = build_writer(feedback_context=feedback_context)

    scout_task   = build_scout_task(scout, industry, job_title, location, limit)
    analyst_task = build_analyst_task(analyst, scout_task)
    writer_task  = build_writer_task(writer, analyst_task)

    crew = Crew(
        agents=[scout, analyst, writer],
        tasks=[scout_task, analyst_task, writer_task],
        process=Process.sequential,
        verbose=True,
    )

    try:
        console.print("\n[bold green]Starting agent pipeline...[/bold green]\n")
        result = crew.kickoff(inputs={
            "industry":  industry,
            "job_title": job_title,
            "location":  location,
            "limit":     limit,
        })

        raw_output = result.raw if hasattr(result, "raw") else str(result)

        # ── Phase 2: Security Guard ─────────────────────────────────────────────
        # Clean up Markdown fences and extract JSON
        cleaned_output = raw_output.strip()
        if cleaned_output.startswith("```"):
            cleaned_output = cleaned_output.split("\n", 1)[-1]
        if cleaned_output.endswith("```"):
            cleaned_output = cleaned_output.rsplit("\n", 1)[0]
        cleaned_output = cleaned_output.strip()

        try:
            leads = json.loads(cleaned_output)
            if not isinstance(leads, list):
                leads = [leads]
        except Exception as e:
            logger.warning(f"Could not parse agent output as JSON: {e}. Wrapping raw output.")
            logger.debug(f"Cleaned output was: {cleaned_output[:200]}...")
            leads = [{"message": raw_output, "full_name": "Unknown", "company": "Unknown"}]

        guard          = SecurityGuard()
        secured_leads  = guard.process(leads)
    except Exception as e:
        logger.error(f"Pipeline run failed: {e}")
        _set_last_run(status="failed", error=str(e),
                      completed_at=datetime.now().isoformat())
        raise

    # ── Save to DB ────────────────────────────────────────────────────────────
    console.print("\n[bold cyan]Pipeline Complete[/bold cyan]\n")
    if run_id:
        save_results(secured_leads, run_id)

    # Print final approved leads
    approved = [l for l in secured_leads if l.get("status") == "approved"]
    console.print(f"\n[bold green]{len(approved)}/{len(secured_leads)} messages approved and saved.[/bold green]")

    # ── Capture for dashboard / API (works even without DB) ──────────────────
    total = len(secured_leads)
    _set_last_run(
        status="completed",
        leads=secured_leads,
        summary={
            "total": total,
            "approved": len(approved),
            "rejected": total - len(approved),
            "avg_security_score": round(
                sum(l.get("security_score", 0) for l in secured_leads) / total
            ) if total else 0,
        },
        completed_at=datetime.now().isoformat(),
    )

    return secured_leads


if __name__ == "__main__":
    run_pipeline()
