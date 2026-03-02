"""
Telegram bot daemon for the job agent.

Commands:
  go apply  — show top matches (score >= MIN_SCORE) and ask to confirm
  confirm   — submit applications to all jobs in the pending list
  cancel    — abort pending applications
  status    — show last scan summary
  help      — list commands

State machine:
  IDLE → "go apply" → AWAITING_CONFIRM → "confirm" → apply → IDLE
                                        → "cancel"  → IDLE
                                        → (5-min timeout) → IDLE

Environment variables:
  TELEGRAM_BOT_TOKEN   required
  TELEGRAM_CHAT_ID     required — only responds to messages from this chat
  TELEGRAM_MIN_SCORE   optional, default 50
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import requests
import yaml

from .applier import apply_to_jobs

logger = logging.getLogger(__name__)

_ROOT         = os.path.join(os.path.dirname(__file__), "..")
_RESULTS_PATH = os.path.join(os.path.dirname(__file__), "results.json")
_OFFSET_PATH  = os.path.join(os.path.dirname(__file__), ".tg_offset")

TELEGRAM_BASE = "https://api.telegram.org/bot{token}"
POLL_TIMEOUT  = 30   # long-poll seconds
CONFIRM_TTL   = 300  # 5 minutes to confirm before auto-cancel


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def _api(token: str, method: str, **kwargs) -> dict:
    url  = f"{TELEGRAM_BASE.format(token=token)}/{method}"
    resp = requests.post(url, timeout=POLL_TIMEOUT + 5, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _send(token: str, chat_id: str, text: str,
          parse_mode: str = "HTML") -> None:
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
# Job helpers
# ---------------------------------------------------------------------------

def _load_top_jobs(min_score: int) -> list:
    if not os.path.exists(_RESULTS_PATH):
        return []
    with open(_RESULTS_PATH) as f:
        data = json.load(f)
    jobs = data.get("jobs", [])
    return [j for j in jobs if j.get("match_score", 0) >= min_score]


def _ats_label(job_id: str) -> str:
    if job_id.startswith("lever::"):
        return "🤖 auto"
    return "🔗 manual"


def _format_job_list(jobs: list) -> str:
    lines = []
    for i, j in enumerate(jobs, 1):
        score = j.get("match_score", 0)
        label = _ats_label(j.get("id", ""))
        lines.append(
            f"{i}. <b>{j['title']}</b>\n"
            f"   🏢 {j['company']} · {score}% · {label}"
        )
    return "\n\n".join(lines)


def _apply_result_msg(results: list) -> str:
    lines = []
    for r in results:
        if r["status"] == "applied":
            icon = "✅"
            detail = "Applied via Lever API"
        elif r["status"] == "manual":
            icon = "🔗"
            detail = f'<a href="{r["url"]}">Apply manually →</a>'
        else:
            icon = "❌"
            detail = r.get("detail", "Unknown error")

        lines.append(f"{icon} <b>{r['title']}</b> @ {r['company']}\n   {detail}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _handle_go_apply(token: str, chat_id: str, min_score: int,
                     state: dict) -> None:
    jobs = _load_top_jobs(min_score)
    if not jobs:
        _send(token, chat_id,
              f"No jobs found with match score ≥ {min_score}%. "
              "Run the scanner first or lower the threshold.")
        return

    # Cap at 10 to avoid spam
    jobs = jobs[:10]
    state["pending_jobs"]   = jobs
    state["confirm_at"]     = time.time()
    state["awaiting_confirm"] = True

    auto   = sum(1 for j in jobs if j.get("id","").startswith("lever::"))
    manual = len(jobs) - auto

    msg = (
        f"📋 <b>Found {len(jobs)} job(s) · {auto} auto-apply · {manual} manual link</b>\n\n"
        f"{_format_job_list(jobs)}\n\n"
        f"🤖 auto = submitted instantly · 🔗 manual = link sent to you\n\n"
        f"Reply <b>confirm</b> to proceed · <b>cancel</b> to abort\n"
        f"<i>(expires in 5 minutes)</i>"
    )
    _send(token, chat_id, msg)


def _handle_confirm(token: str, chat_id: str, state: dict) -> None:
    jobs = state.get("pending_jobs", [])
    if not jobs:
        _send(token, chat_id, "Nothing pending. Send <b>go apply</b> first.")
        return

    _send(token, chat_id, f"⏳ Applying to {len(jobs)} job(s)…")
    results = apply_to_jobs(jobs)

    applied = sum(1 for r in results if r["status"] == "applied")
    manual  = sum(1 for r in results if r["status"] == "manual")
    errors  = sum(1 for r in results if r["status"] == "error")

    summary = (
        f"✅ <b>Done!</b>  Applied: {applied} · Manual: {manual} · Errors: {errors}\n\n"
        f"{_apply_result_msg(results)}"
    )
    _send(token, chat_id, summary)
    state["pending_jobs"]     = []
    state["awaiting_confirm"] = False


def _handle_cancel(token: str, chat_id: str, state: dict) -> None:
    state["pending_jobs"]     = []
    state["awaiting_confirm"] = False
    _send(token, chat_id, "❌ Cancelled. No applications were submitted.")


def _handle_status(token: str, chat_id: str) -> None:
    if not os.path.exists(_RESULTS_PATH):
        _send(token, chat_id, "No scan results yet. Run the scanner first.")
        return
    with open(_RESULTS_PATH) as f:
        data = json.load(f)

    jobs    = data.get("jobs", [])
    top5    = [j for j in jobs if j.get("match_score", 0) >= 50][:5]
    new_ct  = data.get("new_count", 0)
    total   = data.get("total_matched", 0)

    lines = [f"📊 <b>Last scan:</b> {data.get('last_scan','?')}",
             f"Matched: {total} · New: {new_ct}\n"]
    for j in top5:
        lines.append(f"• {j['match_score']}% — {j['title']} @ {j['company']}")

    _send(token, chat_id, "\n".join(lines))


def _handle_help(token: str, chat_id: str) -> None:
    _send(token, chat_id,
          "🤖 <b>Job Agent Commands</b>\n\n"
          "<b>go apply</b> — show top matches and confirm to apply\n"
          "<b>confirm</b>  — submit applications\n"
          "<b>cancel</b>   — abort pending applications\n"
          "<b>status</b>   — show last scan summary\n"
          "<b>help</b>     — this message")


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def run_bot() -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise EnvironmentError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set."
        )

    min_score = int(os.environ.get("TELEGRAM_MIN_SCORE", "50"))
    offset    = _load_offset()

    state = {
        "awaiting_confirm": False,
        "pending_jobs":     [],
        "confirm_at":       0.0,
    }

    logger.info("Bot started. Polling for messages (min_score=%d%%)", min_score)
    _send(token, chat_id,
          "✈ <b>Job Agent online.</b> Send <b>help</b> for commands.")

    while True:
        # Auto-expire pending confirmation after 5 minutes
        if state["awaiting_confirm"] and (time.time() - state["confirm_at"]) > CONFIRM_TTL:
            state["awaiting_confirm"] = False
            state["pending_jobs"]     = []
            _send(token, chat_id, "⏰ Confirmation timed out. No applications submitted.")

        updates = _get_updates(token, offset)

        for update in updates:
            offset = update["update_id"] + 1
            _save_offset(offset)

            msg = update.get("message", {})
            # Only respond to the authorised chat
            if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
                continue

            text = (msg.get("text") or "").strip().lower()
            logger.info("Received: %r", text)

            if text in ("go apply", "/go_apply", "/apply"):
                _handle_go_apply(token, chat_id, min_score, state)

            elif text == "confirm" and state["awaiting_confirm"]:
                _handle_confirm(token, chat_id, state)

            elif text == "cancel":
                _handle_cancel(token, chat_id, state)

            elif text in ("status", "/status"):
                _handle_status(token, chat_id)

            elif text in ("help", "/help", "/start"):
                _handle_help(token, chat_id)

            elif state["awaiting_confirm"]:
                _send(token, chat_id,
                      "Reply <b>confirm</b> to apply or <b>cancel</b> to abort.")

            else:
                _send(token, chat_id,
                      "Send <b>help</b> to see available commands.")
