"""
CLI entry point: scan for new utility bills.

Usage:
    python utilities_agent/run.py [--dry-run] [--config utilities.yaml]

Options:
    --dry-run       Scan and print results without writing to DB or sending alerts
    --config PATH   Path to utilities.yaml (default: utilities.yaml in project root)
"""

import argparse
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

from utilities_agent.scanner import DEFAULT_CONFIG_PATH, run_scan


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan utility websites for new bills.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do not write to DB or send Telegram alerts")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, metavar="PATH",
                        help="Path to utilities.yaml")
    args = parser.parse_args()

    results = run_scan(config_path=args.config, dry_run=args.dry_run)
    print(json.dumps(results, indent=2))

    if results["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
