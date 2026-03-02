"""
Telegram notifier for new TPM job alerts.

Sends one message per job (score >= threshold) to the configured chat.

Required environment variables:
    TELEGRAM_BOT_TOKEN   Bot token from @BotFather
    TELEGRAM_CHAT_ID     Your personal chat ID

Optional:
    TELEGRAM_MIN_SCORE   Minimum match score to alert on (default: 50)
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


def _format_job(job: dict) -> str:
    score   = job.get("match_score", 0)
    title   = job.get("title", "")
    company = job.get("company", "")
    location = job.get("location") or "Remote / Unspecified"
    url     = job.get("url", "")

    # Score bar (5 blocks)
    filled = round(score / 20)
    bar    = "█" * filled + "░" * (5 - filled)

    return (
        f"🆕 <b>New TPM Match — {score}%</b>\n"
        f"{bar}\n\n"
        f"<b>{title}</b>\n"
        f"🏢 {company}\n"
        f"📍 {location}\n"
        f'🔗 <a href="{url}">Apply →</a>'
    )


def send_job_alerts(new_jobs: list, min_score: int = 50) -> None:
    """
    Send a Telegram message for each new job with match_score >= min_score.
    Silently skips if env vars are not set.
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping Telegram alerts.")
        return

    env_min = os.environ.get("TELEGRAM_MIN_SCORE")
    if env_min:
        try:
            min_score = int(env_min)
        except ValueError:
            pass

    eligible = [j for j in new_jobs if j.get("match_score", 0) >= min_score]
    if not eligible:
        logger.info("No new jobs above %d%% threshold — skipping Telegram.", min_score)
        return

    logger.info("Sending %d Telegram alert(s) (score >= %d%%)", len(eligible), min_score)
    for job in eligible:
        try:
            _send(token, chat_id, _format_job(job))
            logger.debug("Sent alert for: %s @ %s", job.get("title"), job.get("company"))
        except Exception as exc:
            logger.warning("Telegram send failed for %s: %s", job.get("id"), exc)
