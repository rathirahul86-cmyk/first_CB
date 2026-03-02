"""
Telegram notifier for new utility bill alerts.

Sends a one-way message when a new bill is detected, prompting the user
to reply with pay/skip commands to the bot.

Required environment variables:
    TELEGRAM_BOT_TOKEN   Bot token from @BotFather
    TELEGRAM_CHAT_ID     Your personal chat ID
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _send(token: str, chat_id: str, text: str) -> None:
    url = TELEGRAM_API.format(token=token)
    resp = requests.post(url, json={
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=10)
    resp.raise_for_status()


def _format_bill(utility: dict, bill, payment_id: str) -> str:
    name = utility.get("name", utility["id"])
    amount_str = f"${bill.amount:.2f}" if bill.amount is not None else "Unknown"
    due_str    = bill.due_date or "Unknown"
    period_str = bill.bill_period or "Unknown"
    uid = utility["id"]

    return (
        f"💡 New bill detected: <b>{name}</b>\n"
        f"💰 Amount: {amount_str}\n"
        f"📅 Due: {due_str}\n"
        f"📋 Period: {period_str}\n\n"
        f"Reply <code>pay {uid}</code> to pay or <code>skip {uid}</code> to skip."
    )


def send_bill_alert(utility: dict, bill, payment_id: str) -> None:
    """
    Send a Telegram message for a newly detected bill.
    Silently skips if env vars are not set.
    """
    token   = os.environ.get("UTILITIES_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("UTILITIES_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning("UTILITIES_BOT_TOKEN or UTILITIES_CHAT_ID not set — skipping alert.")
        return

    try:
        _send(token, chat_id, _format_bill(utility, bill, payment_id))
        logger.info("Sent bill alert for %s (%s)", utility.get("name", utility["id"]), payment_id)
    except Exception as exc:
        logger.warning("Telegram send failed for %s: %s", payment_id, exc)
