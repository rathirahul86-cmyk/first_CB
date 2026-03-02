"""
CLI entry point for the Telegram job-apply bot.

Usage:
    python -m job_agent.run_bot [--verbose]
"""

import argparse
import logging
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram job-apply bot daemon")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )

    from job_agent.telegram_bot import run_bot
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\nBot stopped.")
        return 0
    except EnvironmentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
