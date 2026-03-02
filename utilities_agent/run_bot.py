"""
CLI entry point: start the Telegram bot daemon.

Usage:
    python utilities_agent/run_bot.py [--config utilities.yaml]

Options:
    --config PATH   Path to utilities.yaml (default: utilities.yaml in project root)

Required environment variables:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

from utilities_agent.scanner import DEFAULT_CONFIG_PATH
from utilities_agent.telegram_bot import run_bot


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the utilities Telegram bot.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, metavar="PATH",
                        help="Path to utilities.yaml")
    args = parser.parse_args()

    run_bot(config_path=args.config)


if __name__ == "__main__":
    main()
