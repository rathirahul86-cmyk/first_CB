"""
SQLite-backed payment history store.

Tracks every bill we have detected and its payment status.

Schema:
    payments (
        id           TEXT PRIMARY KEY,   -- e.g. "pge_2026_03"
        utility_id   TEXT NOT NULL,
        utility_name TEXT NOT NULL,
        amount       REAL,
        due_date     TEXT,
        bill_period  TEXT,
        status       TEXT NOT NULL,      -- pending | paid | skipped | failed
        detected_at  TEXT NOT NULL,      -- ISO 8601 UTC
        paid_at      TEXT                -- NULL until paid
    )

DB location: utilities_agent/payments.db by default.
Override with the UTILITIES_DB_PATH environment variable.
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "payments.db")


def _db_path() -> str:
    return os.environ.get("UTILITIES_DB_PATH", DEFAULT_DB_PATH)


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or _db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Optional[str] = None) -> None:
    """Create the payments table if it does not already exist. Safe to call every run."""
    with _connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id           TEXT PRIMARY KEY,
                utility_id   TEXT NOT NULL,
                utility_name TEXT NOT NULL,
                amount       REAL,
                due_date     TEXT,
                bill_period  TEXT,
                status       TEXT NOT NULL,
                detected_at  TEXT NOT NULL,
                paid_at      TEXT
            )
        """)
        conn.commit()


def is_seen(payment_id: str, path: Optional[str] = None) -> bool:
    """Return True if this payment_id has been recorded before."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM payments WHERE id = ?", (payment_id,)
        ).fetchone()
        return row is not None


def insert_pending(
    payment_id: str,
    utility_id: str,
    utility_name: str,
    amount: Optional[float],
    due_date: Optional[str],
    bill_period: Optional[str],
    path: Optional[str] = None,
) -> None:
    """Insert a new bill as pending. No-op if already exists."""
    now = datetime.now(tz=timezone.utc).isoformat()
    with _connect(path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO payments "
            "(id, utility_id, utility_name, amount, due_date, bill_period, status, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (payment_id, utility_id, utility_name, amount, due_date, bill_period, now),
        )
        conn.commit()


def update_status(
    payment_id: str,
    status: str,
    path: Optional[str] = None,
) -> None:
    """Update status. Sets paid_at timestamp when status is 'paid'."""
    now = datetime.now(tz=timezone.utc).isoformat()
    paid_at = now if status == "paid" else None
    with _connect(path) as conn:
        conn.execute(
            "UPDATE payments SET status = ?, paid_at = ? WHERE id = ?",
            (status, paid_at, payment_id),
        )
        conn.commit()


def get_pending_bills(path: Optional[str] = None) -> list:
    """Return all bills with status 'pending'."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE status = 'pending' ORDER BY detected_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_bill(payment_id: str, path: Optional[str] = None) -> Optional[dict]:
    """Return a single bill by id, or None if not found."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM payments WHERE id = ?", (payment_id,)
        ).fetchone()
        return dict(row) if row else None
