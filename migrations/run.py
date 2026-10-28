"""
Auto-migrate the PostgreSQL schema on startup.

Locally, docker-compose runs database/init.sql for you. In the cloud (Neon /
Supabase / Render Postgres) you get an EMPTY database — no tables — so every
query would fail and the dashboard would show "no data".

This module applies database/init.sql at FastAPI startup. It is safe to run
repeatedly: the whole file uses CREATE TABLE/INDEX IF NOT EXISTS, so it is a
no-op once the schema exists.
"""

import os
from pathlib import Path

from loguru import logger

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "database" / "init.sql"


def migrate() -> bool:
    """Apply database/init.sql. Returns True on success, False if DB unreachable.

    Never raises — a missing database must not crash the web process (Render
    would restart-loop). Callers log and continue with in-memory fallbacks.
    """
    if not _SCHEMA_PATH.is_file():
        logger.warning(f"Schema file not found: {_SCHEMA_PATH}")
        return False

    sql = _SCHEMA_PATH.read_text(encoding="utf-8")

    try:
        from db_config import get_conn, describe_target
    except Exception as e:                      # pragma: no cover
        logger.warning(f"db_config unavailable, skipping migration: {e}")
        return False

    try:
        conn = get_conn()
    except Exception as e:
        # Common on cold start / DB sleeping — don't crash the process.
        logger.warning(f"Migration skipped (DB unreachable at {describe_target()}): {e}")
        return False

    try:
        cur = conn.cursor()
        cur.execute(sql)                        # init.sql is all IF NOT EXISTS
        conn.commit()
        cur.close()
        logger.info(f"Schema up to date at {describe_target()}")
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"Schema migration failed: {e}")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    # `python -m migrations.run` — apply the schema by hand if you ever need to.
    ok = migrate()
    print("Migration:", "OK" if ok else "SKIPPED/FAILED")
    raise SystemExit(0 if ok else 1)
