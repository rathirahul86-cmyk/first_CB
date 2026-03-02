"""
Bill scanner — orchestrates detection across all enabled utilities.

For each enabled utility:
  1. Load credentials from environment variables
  2. Use GenericDriver to login and scrape the current bill (no payment)
  3. If the bill is new, record it in the DB and send a Telegram alert

Usage:
    from utilities_agent.scanner import run_scan
    results = run_scan()
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import yaml

from .db import init_db, insert_pending, is_seen
from .drivers.generic import GenericDriver
from .telegram_notifier import send_bill_alert

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "utilities.yaml")
DEFAULT_DB_PATH     = os.path.join(os.path.dirname(__file__), "payments.db")


def load_config(config_path: Optional[str] = None) -> dict:
    path = config_path or DEFAULT_CONFIG_PATH
    with open(path) as f:
        return yaml.safe_load(f)


def load_credentials(utility_id: str) -> dict:
    """Load username/password from env vars: {ID_UPPER}_USERNAME / _PASSWORD."""
    prefix = utility_id.upper().replace("-", "_")
    username = os.environ.get(f"{prefix}_USERNAME", "")
    password = os.environ.get(f"{prefix}_PASSWORD", "")
    if not username or not password:
        logger.warning(
            "Credentials not set for %s. "
            "Set %s_USERNAME and %s_PASSWORD environment variables.",
            utility_id, prefix, prefix,
        )
    return {"username": username, "password": password}


def make_payment_id(utility: dict, bill) -> str:
    """
    Build a stable, unique payment ID for deduplication.

    Uses bill_period_format from config if available, otherwise falls back
    to current year_month.
    """
    fmt = utility.get("bill_period_format", "%Y_%m")
    period_key = datetime.now(tz=timezone.utc).strftime(fmt)
    return f"{utility['id']}_{period_key}"


def run_scan(
    config_path: Optional[str] = None,
    db_path: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """
    Scan all enabled utilities for new bills.

    Args:
        config_path: Path to utilities.yaml (default: project root)
        db_path:     Path to payments.db (default: utilities_agent/payments.db)
        dry_run:     If True, do not write to DB or send Telegram alerts

    Returns:
        dict with keys: scanned, new_bills, errors, results (list of per-utility dicts)
    """
    config  = load_config(config_path)
    db_path = db_path or DEFAULT_DB_PATH

    if not dry_run:
        init_db(db_path)

    utilities = config.get("utilities", [])
    results   = []
    new_bills = 0
    errors    = 0

    for utility in utilities:
        if not utility.get("enabled", True):
            logger.info("Skipping disabled utility: %s", utility.get("id"))
            continue

        uid  = utility["id"]
        name = utility.get("name", uid)
        result = {"utility_id": uid, "utility_name": name, "status": "ok", "bill": None}

        try:
            creds  = load_credentials(uid)
            driver = GenericDriver(utility, creds, headless=True)
            bill   = driver.check_only()

            payment_id = make_payment_id(utility, bill)
            result["bill"] = {
                "payment_id": payment_id,
                "amount":     bill.amount,
                "due_date":   bill.due_date,
                "bill_period": bill.bill_period,
            }

            already_seen = is_seen(payment_id, db_path) if not dry_run else False

            if not already_seen:
                new_bills += 1
                result["is_new"] = True
                if not dry_run:
                    insert_pending(
                        payment_id,
                        uid,
                        name,
                        bill.amount,
                        bill.due_date,
                        bill.bill_period,
                        db_path,
                    )
                    send_bill_alert(utility, bill, payment_id)
                    logger.info("New bill for %s: $%s due %s", name, bill.amount, bill.due_date)
                else:
                    logger.info("[dry-run] Would record bill for %s: $%s", name, bill.amount)
            else:
                result["is_new"] = False
                logger.info("Bill already seen for %s (%s) — skipping", name, payment_id)

        except Exception as exc:
            logger.error("Error scanning %s: %s", name, exc, exc_info=True)
            result["status"] = "error"
            result["error"]  = str(exc)
            errors += 1

        results.append(result)

    return {
        "scanned":   len(results),
        "new_bills": new_bills,
        "errors":    errors,
        "dry_run":   dry_run,
        "results":   results,
    }
