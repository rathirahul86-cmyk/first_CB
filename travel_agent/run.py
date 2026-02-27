"""
CLI entry point for the travel agent flight price scanner.

Usage:
    python -m travel_agent.run [--dry-run] [--verbose] [--weekly]
                               [--config PATH] [--db PATH]

Options:
    --dry-run    Fetch prices but skip DB writes (still writes results.json).
    --verbose    Enable DEBUG logging.
    --weekly     Alias for a full date-window scan (no-op; scanner always uses full window).
    --config PATH  Path to trips.yaml (default: trips.yaml next to app.py).
    --db PATH      Path to flights.db (default: travel_agent/flights.db).
"""

import argparse
import logging
import os
import sys

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "..", "trips.yaml")
DEFAULT_DB     = os.path.join(os.path.dirname(__file__), "flights.db")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flight price scanner — polls Serpapi Google Flights."
    )
    parser.add_argument("--dry-run",  action="store_true",
                        help="Skip DB writes; still output results.json.")
    parser.add_argument("--verbose",  action="store_true",
                        help="Enable DEBUG logging.")
    parser.add_argument("--weekly",   action="store_true",
                        help="Run a full window scan (same as default; flag for CI clarity).")
    parser.add_argument("--config",   default=DEFAULT_CONFIG, metavar="PATH",
                        help=f"Path to trips.yaml (default: {DEFAULT_CONFIG})")
    parser.add_argument("--db",       default=DEFAULT_DB, metavar="PATH",
                        help=f"Path to SQLite DB (default: {DEFAULT_DB})")
    args = parser.parse_args()

    # Import here so logging is configured inside scanner.run_scan
    from travel_agent.scanner import run_scan
    from travel_agent.notifier import send_alert

    try:
        results = run_scan(
            config_path=args.config,
            db_path=args.db,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: Config file not found: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Send alert email if any trip triggered an alert (skip on dry-run)
    if not args.dry_run:
        try:
            send_alert(results)
        except Exception as exc:
            logger.warning("Alert email failed: %s", exc)

    alerts = sum(1 for r in results if r.get("alert"))
    print(f"\nScan complete. {len(results)} trip(s) scanned, {alerts} alert(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
