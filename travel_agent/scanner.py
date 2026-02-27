"""
Flight price scanner orchestrator.

For each trip in trips.yaml:
  1. Build a list of (outbound_date, return_date) pairs to query.
  2. Fetch flight offers from Serpapi for each date pair.
  3. Select the best offer (cheapest preferred airline, or shortest, or cheapest overall).
  4. Compare against previous scan price to detect drops.
  5. Persist prices and snapshot to SQLite.
  6. Collect results for results.json.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import yaml
from typing import Optional

from travel_agent.db import (
    get_last_price,
    get_price_history,
    init_db,
    record_prices,
    record_snapshot,
)
from travel_agent.fetchers.serpapi import fetch_serpapi_flights

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "..", "trips.yaml")
DEFAULT_DB     = os.path.join(os.path.dirname(__file__), "flights.db")
RESULTS_PATH   = os.path.join(os.path.dirname(__file__), "results.json")


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _date_range(start: str, end: str, interval_days: int) -> list[str]:
    """Return weekly-sampled date strings from start to end (inclusive of last)."""
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    dates = []
    cur = s
    while cur <= e:
        dates.append(cur.isoformat())
        cur += timedelta(days=interval_days)
    # Always include the end date
    if dates and dates[-1] != e.isoformat():
        dates.append(e.isoformat())
    return dates


def _build_date_list(trip: dict) -> list:
    """
    Return list of (outbound_date, return_date | None) pairs to query.

    Modes:
      flexible                       — weekly sample of outbound dates, one-way
      fixed                          — single outbound date, one-way
      fixed_outbound_flexible_return — fixed outbound + iterate return dates
    """
    mode = trip.get("date_mode", "flexible")

    if mode == "flexible":
        outbounds = _date_range(
            trip["date_from"], trip["date_to"],
            trip.get("sample_interval_days", 7),
        )
        return [(d, None) for d in outbounds]

    if mode == "fixed":
        return [(trip["outbound_date"], None)]

    if mode == "fixed_outbound_flexible_return":
        outbound = trip["outbound_date"]
        returns  = _date_range(
            trip["return_date_from"], trip["return_date_to"],
            trip.get("return_sample_interval_days", 2),
        )
        return [(outbound, r) for r in returns]

    logger.warning("Unknown date_mode '%s' for trip %s — skipping.", mode, trip["id"])
    return []


# ---------------------------------------------------------------------------
# Best-flight selection
# ---------------------------------------------------------------------------

def _select_best(flights: list[dict], trip: dict) -> Optional[dict]:
    if not flights:
        return None

    preferred = set(trip.get("preferred_airlines", []))
    sort_by   = trip.get("sort_by", "price")
    priority  = trip.get("airline_priority", {})

    preferred_flights = [f for f in flights if f.get("is_preferred")]
    pool = preferred_flights if preferred_flights else flights

    if sort_by == "duration":
        # Sort by duration ascending, then by airline_priority if set
        def _dur_key(f):
            dur  = f.get("duration_minutes") or 99999
            prio = priority.get(f.get("airline_code", ""), 99)
            return (dur, prio, f.get("price_per_person", 99999))
        return min(pool, key=_dur_key)

    # Default: cheapest
    def _price_key(f):
        price = f.get("price_per_person", 99999)
        prio  = priority.get(f.get("airline_code", ""), 99)
        dur   = f.get("duration_minutes") or 99999
        return (price, prio, dur)

    return min(pool, key=_price_key)


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def run_scan(config_path: str = DEFAULT_CONFIG,
             db_path: str     = DEFAULT_DB,
             dry_run: bool    = False,
             verbose: bool    = False) -> list[dict]:
    """
    Execute a full scan of all trips. Returns the results list.
    """
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)-7s  %(message)s",
        )

    # Load config
    with open(config_path) as fh:
        config = yaml.safe_load(fh)
    trips = config.get("trips", [])
    logger.info("Loaded %d trip(s) from %s", len(trips), config_path)

    init_db(db_path)

    results = []

    for trip in trips:
        trip_id = trip["id"]
        logger.info("── Scanning trip: %s (%s → %s)", trip_id,
                    trip["origin"], trip["destination"])

        all_flights: list[dict] = []
        errors: list[str] = []

        date_pairs = _build_date_list(trip)
        logger.info("   %d date pair(s) to query", len(date_pairs))

        for outbound, ret in date_pairs:
            try:
                flights = fetch_serpapi_flights(trip, outbound, ret)
                logger.info("   %s → %s results", outbound, len(flights))
                all_flights.extend(flights)
            except Exception as exc:
                msg = f"Fetch failed for {outbound}/{ret}: {exc}"
                logger.warning("   %s", msg)
                errors.append(msg)

        best = _select_best(all_flights, trip)
        prev_price = get_last_price(trip_id, db_path)

        # Determine alert
        alert = False
        price_drop_pct = None
        if best:
            price = best["price_per_person"]
            threshold = trip.get("alert_threshold")
            if threshold and price < threshold:
                alert = True
            if prev_price and prev_price > 0:
                drop = (prev_price - price) / prev_price * 100
                price_drop_pct = round(drop, 1)
                if drop > 10:
                    alert = True

        # Persist (skip on dry-run)
        if not dry_run:
            if all_flights:
                record_prices(all_flights, db_path)
            if best:
                record_snapshot(trip_id, best, db_path)
        else:
            logger.info("   [dry-run] skipping DB writes")

        history = get_price_history(trip_id, days=30, db_path=db_path)

        # Top candidates (up to 10 cheapest)
        candidates = sorted(all_flights, key=lambda f: f.get("price_per_person", 99999))[:10]

        result = {
            "trip_id":        trip_id,
            "name":           trip.get("name", trip_id),
            "origin":         trip["origin"],
            "destination":    trip["destination"],
            "travelers":      trip.get("travelers", 1),
            "cabin":          trip.get("cabin", "economy"),
            "preferred_airlines": trip.get("preferred_airlines", []),
            "alert_threshold":    trip.get("alert_threshold"),
            "best":           best,
            "candidates":     candidates,
            "prev_price":     prev_price,
            "price_drop_pct": price_drop_pct,
            "alert":          alert,
            "price_history":  history,
            "errors":         errors,
            "scanned_at":     datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        results.append(result)

        # Summary log
        if best:
            logger.info("   Best: $%.0f/person via %s on %s (alert=%s)",
                        best["price_per_person"], best.get("airline_code", "?"),
                        best.get("outbound_date"), alert)
        else:
            logger.info("   No flights found (errors: %d)", len(errors))

    # Write results.json
    payload = {
        "last_scan":  datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "trip_count": len(results),
        "alert_count": sum(1 for r in results if r.get("alert")),
        "trips":      results,
    }
    if not dry_run:
        with open(RESULTS_PATH, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        logger.info("Results written to %s", RESULTS_PATH)
    else:
        logger.info("[dry-run] Would write %d trip results to results.json", len(results))
        # Still write for inspection during dry-run
        with open(RESULTS_PATH, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)

    return results
