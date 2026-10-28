"""
Single source of truth for PostgreSQL connections — cloud-ready.

Why this exists
---------------
Every module used to build its own psycopg2 config from POSTGRES_* env vars.
That worked on localhost but broke the moment the app was deployed:

  * Neon / Supabase / Render Postgres hand you ONE connection string
    (DATABASE_URL) — not five separate vars.
  * Managed Postgres REQUIRES SSL. A plain connect() is refused, so the
    dashboard silently showed "no data".
  * Free-tier Postgres cold-starts can take >10s; psycopg2's default
    connect_timeout is effectively "wait forever", which made Render kill
    the instance during health checks.

This module accepts EITHER style of configuration, applies a sane SSL mode
and a bounded connect timeout, and is used by every DB consumer so the app
behaves identically on localhost and in the cloud.

Priority (first match wins):
  1. DATABASE_URL / POSTGRES_URL / POSTGRES_EXTERNAL_URL  (Neon, Supabase, Render)
  2. POSTGRES_HOST + POSTGRES_PORT + POSTGRES_DB + POSTGRES_USER + POSTGRES_PASSWORD

SSL rules:
  * Explicit DATABASE_URL query params (e.g. ?sslmode=require) are honoured.
  * A remote host (not localhost/127.0.0.1) defaults to require  — managed
    Postgres always needs it.
  * localhost stays "prefer" so local Docker Postgres keeps working.
  * Override any time with POSTGRES_SSLMODE (disable | allow | prefer | require).
"""

import os
from typing import Any, Dict
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv

load_dotenv()

# Hostnames that are "local" — SSL is optional there.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}

# Env vars that may carry a full connection string, in priority order.
_URL_ENV_VARS = ("DATABASE_URL", "POSTGRES_URL", "POSTGRES_EXTERNAL_URL")


def _first_url() -> str:
    for var in _URL_ENV_VARS:
        val = os.getenv(var, "").strip()
        if val:
            return val
    return ""


def db_kwargs() -> Dict[str, Any]:
    """Build a psycopg2.connect() keyword dict from the environment.

    Works with DATABASE_URL (cloud) or the individual POSTGRES_* vars (local).
    Never raises — callers keep their existing try/except handling.
    """
    url = _first_url()

    if url:
        # Heroku-style "postgres://"/"postgresql://" is not understood by
        # psycopg2's URI parser in every version — normalise it.
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]

        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        host = parsed.hostname or "localhost"
        port = parsed.port or 5432
        dbname = (parsed.path or "/").lstrip("/") or os.getenv("POSTGRES_DB", "agentflow_db")
        user = parsed.username or ""
        password = parsed.password or ""

        # SSL: explicit ?sslmode= wins, else require for remote, prefer for local.
        sslmode = (query.get("sslmode") or [None])[0]
    else:
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = int(os.getenv("POSTGRES_PORT", 5432))
        dbname = os.getenv("POSTGRES_DB", "agentflow_db")
        user = os.getenv("POSTGRES_USER", "agentflow")
        password = os.getenv("POSTGRES_PASSWORD", "agentflow_secret")
        sslmode = None  # decided below

    # Env-var override always wins (lets you force-disable SSL in an emergency).
    sslmode = os.getenv("POSTGRES_SSLMODE", "").strip() or sslmode

    if not sslmode:
        is_local = host.strip().lower() in _LOCAL_HOSTS
        sslmode = "prefer" if is_local else "require"

    kwargs: Dict[str, Any] = {
        "host": host,
        "port": int(port),
        "dbname": dbname,
        "user": user,
        "password": password,
        "sslmode": sslmode,
        # Bounded wait: free-tier Postgres cold-starts, but we must never hang
        # a uvicorn worker / health check forever.
        "connect_timeout": int(os.getenv("POSTGRES_CONNECT_TIMEOUT", "10")),
        # Render/Neon poolers drop idle conns; don't reuse stale ones.
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }
    return kwargs


def get_conn():
    """Open a PostgreSQL connection using db_kwargs(). Caller closes it."""
    import psycopg2
    return psycopg2.connect(**db_kwargs())


def db_available() -> bool:
    """True if a connection can be opened right now (used for health/status)."""
    try:
        conn = get_conn()
        conn.close()
        return True
    except Exception:
        return False


def describe_target() -> str:
    """Short, credential-free description of where we're pointed — for logs."""
    kw = db_kwargs()
    return f"{kw['user']}@{kw['host']}:{kw['port']}/{kw['dbname']} ssl={kw['sslmode']}"
