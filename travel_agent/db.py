"""
SQLite persistence for flight price history.

Tables
------
flight_prices   — one row per flight offer per scan
price_snapshots — one summary row per trip per scan
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DB = os.path.join(os.path.dirname(__file__), "flights.db")

_CREATE_PRICES = """
CREATE TABLE IF NOT EXISTS flight_prices (
    id               TEXT NOT NULL,
    trip_id          TEXT NOT NULL,
    origin           TEXT,
    destination      TEXT,
    outbound_date    TEXT,
    return_date      TEXT,
    price_per_person REAL,
    total_price      REAL,
    duration_minutes INTEGER,
    stops            INTEGER,
    airline          TEXT,
    airline_code     TEXT,
    flight_number    TEXT,
    url              TEXT,
    scanned_at       TEXT NOT NULL,
    PRIMARY KEY (id, scanned_at)
)
"""

_CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS price_snapshots (
    trip_id          TEXT NOT NULL,
    scanned_at       TEXT NOT NULL,
    best_price       REAL,
    best_outbound_date TEXT,
    best_airline     TEXT,
    best_airline_code TEXT,
    best_duration    INTEGER,
    PRIMARY KEY (trip_id, scanned_at)
)
"""


def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: str = DEFAULT_DB) -> None:
    with _connect(db_path) as con:
        con.execute(_CREATE_PRICES)
        con.execute(_CREATE_SNAPSHOTS)
    logger.debug("DB initialised at %s", db_path)


def record_prices(flights: list[dict], db_path: str = DEFAULT_DB) -> None:
    if not flights:
        return
    rows = [
        (
            f["id"], f["trip_id"], f["origin"], f["destination"],
            f["outbound_date"], f.get("return_date"),
            f["price_per_person"], f["total_price"],
            f.get("duration_minutes"), f.get("stops"),
            f.get("airline"), f.get("airline_code"), f.get("flight_number"),
            f.get("url"), f["scanned_at"],
        )
        for f in flights
    ]
    with _connect(db_path) as con:
        con.executemany(
            """INSERT OR IGNORE INTO flight_prices
               (id, trip_id, origin, destination, outbound_date, return_date,
                price_per_person, total_price, duration_minutes, stops,
                airline, airline_code, flight_number, url, scanned_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    logger.debug("Recorded %d flight prices", len(rows))


def record_snapshot(trip_id: str, best: dict, db_path: str = DEFAULT_DB) -> None:
    scanned_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect(db_path) as con:
        con.execute(
            """INSERT OR REPLACE INTO price_snapshots
               (trip_id, scanned_at, best_price, best_outbound_date,
                best_airline, best_airline_code, best_duration)
               VALUES (?,?,?,?,?,?,?)""",
            (
                trip_id, scanned_at,
                best.get("price_per_person"),
                best.get("outbound_date"),
                best.get("airline"),
                best.get("airline_code"),
                best.get("duration_minutes"),
            ),
        )
    logger.debug("Recorded snapshot for %s", trip_id)


def get_last_price(trip_id: str, db_path: str = DEFAULT_DB) -> Optional[float]:
    """Return the best_price from the most recent snapshot for this trip."""
    with _connect(db_path) as con:
        row = con.execute(
            """SELECT best_price FROM price_snapshots
               WHERE trip_id = ?
               ORDER BY scanned_at DESC LIMIT 1""",
            (trip_id,),
        ).fetchone()
    return float(row["best_price"]) if row else None


def get_price_history(trip_id: str, days: int = 30,
                      db_path: str = DEFAULT_DB) -> list[dict]:
    """Return list of {scanned_at, best_price} for the last N days."""
    with _connect(db_path) as con:
        rows = con.execute(
            """SELECT scanned_at, best_price FROM price_snapshots
               WHERE trip_id = ?
                 AND scanned_at >= datetime('now', ?)
               ORDER BY scanned_at ASC""",
            (trip_id, f"-{days} days"),
        ).fetchall()
    return [{"scanned_at": r["scanned_at"], "best_price": r["best_price"]}
            for r in rows]
