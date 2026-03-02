"""
Telegram bot daemon for the utilities agent.

Commands:
  status          — show all pending bills from DB
  scan            — run a fresh scan for new bills
  pay [id]        — show bill details and enter confirmation state
  skip [id]       — mark bill as skipped
  confirm [id]    — execute payment (only valid after 'pay [id]')
  cancel          — abort pending confirmation
  help            — list commands

State machine:
  IDLE → "pay [id]" → AWAITING_CONFIRM(id) → "confirm [id]" → pay → IDLE
                                             → "cancel"      → IDLE
                                             → (5-min timeout) → IDLE

Environment variables:
  TELEGRAM_BOT_TOKEN   required
  TELEGRAM_CHAT_ID     required — only responds to messages from this chat
"""

import logging
import os
import time
from typing import Optional

import requests

from .db import get_bill, get_pending_bills, init_db, update_status
from .drivers.generic import GenericDriver
from .scanner import load_config, load_credentials, run_scan

logger = logging.getLogger(__name__)

_OFFSET_PATH = os.path.join(os.path.dirname(__file__), ".tg_offset")

TELEGRAM_BASE = "https://api.telegram.org/bot{token}"
POLL_TIMEOUT  = 30    # long-poll seconds
CONFIRM_TTL   = 300   # 5 minutes to confirm before auto-cancel

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "utilities.yaml")
DEFAULT_DB_PATH     = os.path.join(os.path.dirname(__file__), "payments.db")


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def _api(token: str, method: str, **kwargs) -> dict:
    url  = f"{TELEGRAM_BASE.format(token=token)}/{method}"
    resp = requests.post(url, timeout=POLL_TIMEOUT + 5, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _send(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> None:
    _api(token, "sendMessage", json={
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    })


def _get_updates(token: str, offset: int) -> list:
    try:
        data = _api(token, "getUpdates", json={
            "offset":  offset,
            "timeout": POLL_TIMEOUT,
            "allowed_updates": ["message"],
        })
        return data.get("result", [])
    except Exception as exc:
        logger.warning("getUpdates failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Offset persistence
# ---------------------------------------------------------------------------

def _load_offset() -> int:
    try:
        return int(open(_OFFSET_PATH).read().strip())
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    with open(_OFFSET_PATH, "w") as f:
        f.write(str(offset))


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_bill_row(bill: dict) -> str:
    amount = f"${bill['amount']:.2f}" if bill.get("amount") is not None else "Unknown"
    due    = bill.get("due_date") or "Unknown"
    period = bill.get("bill_period") or "Unknown"
    return (
        f"• <b>{bill['utility_name']}</b> — {amount}\n"
        f"  Due: {due} · Period: {period}\n"
        f"  ID: <code>{bill['id']}</code>"
    )


def _format_pending_list(bills: list) -> str:
    if not bills:
        return "No pending bills."
    return "\n\n".join(_format_bill_row(b) for b in bills)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _handle_status(token: str, chat_id: str, db_path: str) -> None:
    bills = get_pending_bills(db_path)
    msg = f"📋 <b>Pending Bills ({len(bills)})</b>\n\n{_format_pending_list(bills)}"
    _send(token, chat_id, msg)


def _handle_scan(
    token: str, chat_id: str, config_path: str, db_path: str
) -> None:
    _send(token, chat_id, "🔍 Scanning for new bills…")
    try:
        summary = run_scan(config_path=config_path, db_path=db_path)
        new_ct  = summary["new_bills"]
        errors  = summary["errors"]
        scanned = summary["scanned"]
        msg = (
            f"✅ Scan complete: {scanned} checked, {new_ct} new, {errors} error(s).\n"
            + ("New bills sent as alerts above." if new_ct else "No new bills found.")
        )
        _send(token, chat_id, msg)
    except Exception as exc:
        _send(token, chat_id, f"❌ Scan failed: {exc}")


def _handle_pay(
    token: str, chat_id: str, utility_id: str, state: dict, db_path: str
) -> None:
    """Show bill details and enter AWAITING_CONFIRM state."""
    # Find the pending bill for this utility
    bills = get_pending_bills(db_path)
    matching = [b for b in bills if b["utility_id"] == utility_id or b["id"] == utility_id]

    if not matching:
        _send(token, chat_id,
              f"No pending bill found for <code>{utility_id}</code>. "
              "Run <b>status</b> to see pending bills.")
        return

    bill = matching[0]
    amount = f"${bill['amount']:.2f}" if bill.get("amount") is not None else "Unknown"
    state["awaiting_confirm"]  = True
    state["pending_utility_id"] = bill["id"]
    state["confirm_at"]        = time.time()

    msg = (
        f"💳 <b>Confirm payment for {bill['utility_name']}</b>\n\n"
        f"Amount: <b>{amount}</b>\n"
        f"Due: {bill.get('due_date') or 'Unknown'}\n"
        f"Period: {bill.get('bill_period') or 'Unknown'}\n"
        f"Bill ID: <code>{bill['id']}</code>\n\n"
        f"Reply <code>confirm {bill['utility_id']}</code> to pay · "
        f"<code>cancel</code> to abort\n"
        f"<i>(expires in 5 minutes)</i>"
    )
    _send(token, chat_id, msg)


def _handle_confirm(
    token: str,
    chat_id: str,
    confirm_id: str,
    state: dict,
    config_path: str,
    db_path: str,
) -> None:
    """Execute payment for the confirmed bill."""
    pending_id = state.get("pending_utility_id")

    if not state.get("awaiting_confirm") or not pending_id:
        _send(token, chat_id, "Nothing pending. Send <b>pay [utility_id]</b> first.")
        return

    bill = get_bill(pending_id, db_path)
    if not bill:
        _send(token, chat_id, f"Bill <code>{pending_id}</code> not found in DB.")
        state["awaiting_confirm"] = False
        return

    # Allow confirming by either full payment_id or utility_id
    if confirm_id and confirm_id not in (bill["id"], bill["utility_id"]):
        _send(token, chat_id,
              f"ID mismatch. Expected <code>{bill['utility_id']}</code> or "
              f"<code>{bill['id']}</code>. Got <code>{confirm_id}</code>.")
        return

    _send(token, chat_id, f"⏳ Paying <b>{bill['utility_name']}</b>…")

    # Load config + driver
    try:
        config   = load_config(config_path)
        utl_cfg  = next(u for u in config["utilities"] if u["id"] == bill["utility_id"])
        creds    = load_credentials(bill["utility_id"])
        driver   = GenericDriver(utl_cfg, creds, headless=True)
        driver.login()
        driver.get_bill_info()
        result   = driver.pay_bill()
    except StopIteration:
        result_msg = f"Utility <code>{bill['utility_id']}</code> not found in utilities.yaml."
        _send(token, chat_id, f"❌ {result_msg}")
        state["awaiting_confirm"] = False
        return
    except Exception as exc:
        _send(token, chat_id, f"❌ Payment error: {exc}")
        update_status(pending_id, "failed", db_path)
        state["awaiting_confirm"]  = False
        state["pending_utility_id"] = None
        return

    if result.success:
        update_status(pending_id, "paid", db_path)
        _send(token, chat_id, f"✅ {result.message}")
    else:
        update_status(pending_id, "failed", db_path)
        _send(token, chat_id, f"❌ {result.message}")

    state["awaiting_confirm"]  = False
    state["pending_utility_id"] = None


def _handle_skip(
    token: str, chat_id: str, utility_id: str, db_path: str, state: dict
) -> None:
    bills    = get_pending_bills(db_path)
    matching = [b for b in bills if b["utility_id"] == utility_id or b["id"] == utility_id]

    if not matching:
        _send(token, chat_id, f"No pending bill found for <code>{utility_id}</code>.")
        return

    bill = matching[0]
    update_status(bill["id"], "skipped", db_path)

    # Clear confirm state if it was for this bill
    if state.get("pending_utility_id") == bill["id"]:
        state["awaiting_confirm"]  = False
        state["pending_utility_id"] = None

    _send(token, chat_id,
          f"⏭ Skipped bill for <b>{bill['utility_name']}</b> "
          f"(<code>{bill['id']}</code>).")


def _handle_cancel(token: str, chat_id: str, state: dict) -> None:
    state["awaiting_confirm"]  = False
    state["pending_utility_id"] = None
    _send(token, chat_id, "❌ Cancelled. No payment was made.")


def _handle_help(token: str, chat_id: str) -> None:
    _send(token, chat_id,
          "💡 <b>Utilities Agent Commands</b>\n\n"
          "<b>status</b>          — show pending bills\n"
          "<b>scan</b>            — check for new bills now\n"
          "<b>pay [id]</b>        — initiate payment (e.g. <code>pay pge</code>)\n"
          "<b>confirm [id]</b>    — confirm and execute payment\n"
          "<b>skip [id]</b>       — mark bill as skipped\n"
          "<b>cancel</b>          — abort pending confirmation\n"
          "<b>help</b>            — this message")


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def run_bot(
    config_path: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    token   = os.environ.get("UTILITIES_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("UTILITIES_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise EnvironmentError(
            "UTILITIES_BOT_TOKEN and UTILITIES_CHAT_ID must be set "
            "(falls back to TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)."
        )

    config_path = config_path or DEFAULT_CONFIG_PATH
    db_path     = db_path or DEFAULT_DB_PATH
    init_db(db_path)

    offset = _load_offset()
    state  = {
        "awaiting_confirm":  False,
        "pending_utility_id": None,
        "confirm_at":        0.0,
    }

    logger.info("Utilities bot started. Polling for messages.")
    _send(token, chat_id,
          "💡 <b>Utilities Agent online.</b> Send <b>help</b> for commands.")

    while True:
        # Auto-expire pending confirmation after 5 minutes
        if state["awaiting_confirm"] and (time.time() - state["confirm_at"]) > CONFIRM_TTL:
            state["awaiting_confirm"]  = False
            state["pending_utility_id"] = None
            _send(token, chat_id, "⏰ Confirmation timed out. No payment was made.")

        updates = _get_updates(token, offset)

        for update in updates:
            offset = update["update_id"] + 1
            _save_offset(offset)

            msg = update.get("message", {})
            if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
                continue

            text = (msg.get("text") or "").strip().lower()
            logger.info("Received: %r", text)

            # Parse command and optional argument
            parts = text.split(None, 1)
            cmd   = parts[0] if parts else ""
            arg   = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("status", "/status"):
                _handle_status(token, chat_id, db_path)

            elif cmd in ("scan", "/scan"):
                _handle_scan(token, chat_id, config_path, db_path)

            elif cmd in ("pay", "/pay") and arg:
                _handle_pay(token, chat_id, arg, state, db_path)

            elif cmd in ("confirm", "/confirm"):
                _handle_confirm(token, chat_id, arg, state, config_path, db_path)

            elif cmd in ("skip", "/skip") and arg:
                _handle_skip(token, chat_id, arg, db_path, state)

            elif cmd in ("cancel", "/cancel"):
                _handle_cancel(token, chat_id, state)

            elif cmd in ("help", "/help", "/start"):
                _handle_help(token, chat_id)

            elif state["awaiting_confirm"]:
                uid = state.get("pending_utility_id", "")
                _send(token, chat_id,
                      f"Reply <code>confirm {uid.split('_')[0] if uid else ''}</code> "
                      "to pay or <code>cancel</code> to abort.")

            else:
                _send(token, chat_id, "Send <b>help</b> to see available commands.")
