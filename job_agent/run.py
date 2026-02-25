"""
CLI entry point for the TPM job scanning agent.

Usage:
    python -m job_agent.run                          # normal run
    python -m job_agent.run --dry-run                # fetch + filter only, no DB/email
    python -m job_agent.run --dry-run --verbose      # with debug logging
    python -m job_agent.run --config /path/to/companies.yaml
    python -m job_agent.run --db /path/to/jobs_seen.db

Environment variables (required for email):
    GMAIL_USER          sender Gmail address
    GMAIL_APP_PASSWORD  Gmail App Password (not your account password)
    GMAIL_TO            recipient address

Exit codes:
    0   success (even if no new jobs found)
    1   partial failure (some fetchers errored, run completed)
    2   fatal error (e.g., companies.yaml not found)
"""

import argparse
import logging
import sys

from .scanner import run_scan


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan ATS job boards for TPM roles at AI companies."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and filter without writing to DB or sending email.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug-level logging.",
    )
    parser.add_argument(
        "--config", metavar="PATH",
        help="Path to companies.yaml (default: auto-detected).",
    )
    parser.add_argument(
        "--db", metavar="PATH",
        help="Path to SQLite dedup DB (default: job_agent/jobs_seen.db).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        result = run_scan(
            config_path=args.config,
            db_path=args.db,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        logging.error("Config file not found: %s", exc)
        sys.exit(2)
    except Exception as exc:
        logging.error("Fatal error: %s", exc)
        sys.exit(2)

    # ── Summary ────────────────────────────────────────────────────────────
    print("\n" + "─" * 42)
    print(f"  Fetched   : {result['total_fetched']} jobs")
    print(f"  Matched   : {result['total_filtered']} (keyword/location filter)")
    print(f"  New       : {result['new_jobs']} (not seen before)")
    print(f"  Errors    : {len(result['errors'])}")
    print("─" * 42)

    if result["errors"]:
        print("\nErrors:")
        for err in result["errors"]:
            print(f"  • {err}")

    if args.dry_run and result.get("jobs"):
        print(f"\nMatching jobs ({len(result['jobs'])}):\n")
        current_company = None
        for j in sorted(result["jobs"], key=lambda x: x["company"]):
            if j["company"] != current_company:
                current_company = j["company"]
                print(f"  [{current_company}]")
            loc = j.get("location") or "Remote/Unspecified"
            print(f"    {j['title']}  —  {loc}")
            print(f"    {j['url']}")
            print()

    sys.exit(1 if result["errors"] else 0)


if __name__ == "__main__":
    main()
