"""
SQLite-backed deduplication store.

Tracks every job we have ever seen so we only notify once per new posting.

Schema:
    seen_jobs (
        job_id     TEXT PRIMARY KEY,   -- e.g. "greenhouse::openai::123456"
        title      TEXT,
        company    TEXT,
        url        TEXT,
        first_seen TEXT                -- ISO 8601 UTC timestamp
    )

DB location: job_agent/jobs_seen.db by default.
Override with the DB_PATH environment variable, e.g.:
    DB_PATH=/tmp/test.db python -m job_agent.run --dry-run
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "jobs_seen.db")


def _db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or _db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Optional[str] = None) -> None:
    """Create the seen_jobs table if it does not already exist. Safe to call every run."""
    with _connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_jobs (
                job_id     TEXT PRIMARY KEY,
                title      TEXT,
                company    TEXT,
                url        TEXT,
                first_seen TEXT
            )
        """)
        conn.commit()


def is_seen(job_id: str, path: Optional[str] = None) -> bool:
    """Return True if this job_id has been recorded before."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row is not None


def mark_seen_batch(jobs: list, path: Optional[str] = None) -> None:
    """
    Insert multiple jobs in a single transaction.
    Uses INSERT OR IGNORE to preserve the original first_seen timestamp
    if a job somehow appears in two consecutive runs.
    """
    now = datetime.now(tz=timezone.utc).isoformat()
    rows = [(j["id"], j["title"], j["company"], j["url"], now) for j in jobs]
    with _connect(path) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO seen_jobs (job_id, title, company, url, first_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()


def count_seen(path: Optional[str] = None) -> int:
    """Return total number of jobs in the dedup store."""
    with _connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
