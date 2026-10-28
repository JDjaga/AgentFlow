"""
Scheduled-run store — Design A ("n8n polls a to-do list")
=========================================================
The dashboard lets a user schedule a pipeline run ("8:00 AM daily" or
"in 30 minutes"). That job is stored here. n8n fires every few minutes,
calls GET /jobs/due, triggers anything that's due, then marks it done.

Storage strategy:
  * PostgreSQL when reachable  → durable, multi-user (Render + Neon).
  * In-memory fallback         → so the demo works with NO database up
                                 (local testing / free-tier cold start).

The n8n poll cadence bounds the precision to that interval (e.g. every
5 min ⇒ an "8:00" job runs between 8:00–8:05). That's intended.
"""

import os
import threading
import psycopg2
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from loguru import logger
from dotenv import load_dotenv

load_dotenv()


# ─── DB helper ────────────────────────────────────────────────────────────────

def _connect():
    # Cloud-ready: honours DATABASE_URL (Neon/Supabase) or POSTGRES_* vars,
    # with sslmode + bounded connect timeout. See db_config.py.
    from db_config import get_conn
    return get_conn()


def _db_available() -> bool:
    try:
        conn = _connect()
        conn.close()
        return True
    except Exception:
        return False


def _ensure_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_runs (
            id                SERIAL PRIMARY KEY,
            industry          VARCHAR(255),
            job_title         VARCHAR(255),
            location          VARCHAR(255),
            limit_count       INTEGER DEFAULT 5,
            schedule_kind     VARCHAR(20) DEFAULT 'once',  -- once | daily
            time_of_day       VARCHAR(8),                  -- '08:00' for daily
            run_at            TIMESTAMP,                   -- next fire time
            status            VARCHAR(20) DEFAULT 'pending',-- pending|triggered|failed
            trigger_count     INTEGER DEFAULT 0,
            created_at        TIMESTAMP DEFAULT NOW(),
            last_triggered_at TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()


# ─── In-memory fallback ───────────────────────────────────────────────────────

_MEM_LOCK = threading.Lock()
_MEM_JOBS: List[Dict[str, Any]] = []
_MEM_SEQ = {"id": 0}


# ─── Public API ───────────────────────────────────────────────────────────────

def create_job(industry: str, job_title: str, location: str, limit: int,
               schedule_kind: str, run_at: datetime,
               time_of_day: Optional[str] = None) -> Dict[str, Any]:
    schedule_kind = "daily" if schedule_kind == "daily" else "once"
    if _db_available():
        try:
            conn = _connect()
            _ensure_table(conn)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO scheduled_runs
                  (industry, job_title, location, limit_count,
                   schedule_kind, time_of_day, run_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                RETURNING id, run_at, schedule_kind, time_of_day, status, trigger_count
            """, (industry, job_title, location, limit,
                  schedule_kind, time_of_day, run_at))
            row = cur.fetchone()
            conn.commit()
            cur.close()
            conn.close()
            job = {
                "id": row[0], "industry": industry, "job_title": job_title,
                "location": location, "limit": limit,
                "schedule_kind": row[2], "time_of_day": row[3],
                "run_at": row[1].isoformat() if row[1] else None,
                "status": row[4], "trigger_count": row[5],
                "store": "postgres",
            }
            logger.info(f"Scheduled job #{job['id']} stored in Postgres (kind={schedule_kind})")
            return job
        except Exception as e:
            logger.warning(f"Postgres schedule insert failed ({e}) — using memory.")

    with _MEM_LOCK:
        _MEM_SEQ["id"] += 1
        job = {
            "id": _MEM_SEQ["id"], "industry": industry, "job_title": job_title,
            "location": location, "limit": limit,
            "schedule_kind": schedule_kind, "time_of_day": time_of_day,
            "run_at": run_at.isoformat(), "status": "pending",
            "trigger_count": 0, "store": "memory",
        }
        _MEM_JOBS.append(job)
    logger.info(f"Scheduled job #{job['id']} stored in MEMORY (kind={schedule_kind})")
    return job


def list_jobs(limit: int = 50) -> List[Dict[str, Any]]:
    if _db_available():
        try:
            conn = _connect()
            _ensure_table(conn)
            cur = conn.cursor()
            cur.execute("""
                SELECT id, industry, job_title, location, limit_count,
                       schedule_kind, time_of_day, run_at, status, trigger_count
                FROM scheduled_runs ORDER BY run_at ASC NULLS LAST LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cur.close()
            conn.close()
            return [
                {"id": r[0], "industry": r[1], "job_title": r[2], "location": r[3],
                 "limit": r[4], "schedule_kind": r[5], "time_of_day": r[6],
                 "run_at": r[7].isoformat() if r[7] else None,
                 "status": r[8], "trigger_count": r[9], "store": "postgres"}
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"Postgres list_jobs failed ({e}) — using memory.")

    with _MEM_LOCK:
        return [dict(j) for j in _MEM_JOBS]


def due_jobs(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Jobs whose run_at <= now and still pending (what n8n should fire)."""
    now = now or datetime.now()
    if _db_available():
        try:
            conn = _connect()
            _ensure_table(conn)
            cur = conn.cursor()
            cur.execute("""
                SELECT id, industry, job_title, location, limit_count,
                       schedule_kind, time_of_day, run_at, status, trigger_count
                FROM scheduled_runs
                WHERE status = 'pending' AND run_at IS NOT NULL AND run_at <= %s
                ORDER BY run_at ASC
            """, (now,))
            rows = cur.fetchall()
            cur.close()
            conn.close()
            return [
                {"id": r[0], "industry": r[1], "job_title": r[2], "location": r[3],
                 "limit": r[4], "schedule_kind": r[5], "time_of_day": r[6],
                 "run_at": r[7].isoformat() if r[7] else None,
                 "status": r[8], "trigger_count": r[9], "store": "postgres"}
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"Postgres due_jobs failed ({e}) — using memory.")

    with _MEM_LOCK:
        out = []
        for j in _MEM_JOBS:
            if j["status"] != "pending":
                continue
            try:
                if datetime.fromisoformat(j["run_at"]) <= now:
                    out.append(dict(j))
            except Exception:
                continue
        return out


def mark_triggered(job_id: int, failed: bool = False) -> bool:
    """
    Mark a job as fired. 'once' jobs → done. 'daily' jobs → roll run_at to the
    next day's time_of_day and go back to pending (so they recur).
    """
    if _db_available():
        try:
            conn = _connect()
            _ensure_table(conn)
            cur = conn.cursor()
            if failed:
                cur.execute(
                    "UPDATE scheduled_runs SET status='failed', last_triggered_at=NOW() WHERE id=%s",
                    (job_id,))
            else:
                cur.execute("""
                    UPDATE scheduled_runs
                    SET trigger_count = trigger_count + 1,
                        last_triggered_at = NOW(),
                        run_at = CASE
                          WHEN schedule_kind='daily' AND time_of_day IS NOT NULL THEN
                            CASE WHEN run_at + interval '1 day' > NOW()
                                 THEN (run_at + interval '1 day')
                                 ELSE NOW() + ((time_of_day::time - NOW()::time) + interval '1 day')
                            END
                          ELSE run_at END,
                        status = CASE WHEN schedule_kind='daily' THEN 'pending' ELSE 'done' END
                    WHERE id=%s RETURNING status, run_at
                """, (job_id,))
                cur.fetchone()
            conn.commit()
            cur.close()
            conn.close()
            return True
        except Exception as e:
            logger.warning(f"Postgres mark_triggered failed ({e}) — using memory.")

    with _MEM_LOCK:
        for j in _MEM_JOBS:
            if j["id"] == job_id:
                j["trigger_count"] = j.get("trigger_count", 0) + 1
                if failed:
                    j["status"] = "failed"
                elif j["schedule_kind"] == "daily":
                    # roll to next occurrence of time_of_day
                    try:
                        tod = datetime.strptime(j.get("time_of_day") or "09:00", "%H:%M")
                        nxt = datetime.combine((now_date := datetime.now()).date(), tod.time())
                        if nxt <= datetime.now():
                            nxt += timedelta(days=1)
                        j["run_at"] = nxt.isoformat()
                        j["status"] = "pending"
                    except Exception:
                        j["status"] = "done"
                else:
                    j["status"] = "done"
                return True
    return False
