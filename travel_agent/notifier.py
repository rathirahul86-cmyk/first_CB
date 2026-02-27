"""
Gmail SMTP email notifier for flight price alerts.

Required environment variables:
    EMAIL_USER      Gmail address (sender)
    EMAIL_PASSWORD  Gmail App Password
    EMAIL_TO        Recipient address

Only fires if at least one trip has alert=True.
"""

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def _pct_str(pct) -> str:
    if pct is None:
        return ""
    sign = "↓" if pct < 0 else "↑"
    return f" ({sign}{abs(pct):.1f}%)"


def _render_html(alert_trips: list[dict], run_time: str) -> str:
    rows = []
    for t in alert_trips:
        best = t.get("best") or {}
        price = best.get("price_per_person")
        airline = best.get("airline", "")
        date = best.get("outbound_date", "")
        pct = t.get("price_drop_pct")
        pct_str = _pct_str(pct)

        rows.append(f"""
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #eee;">
            <strong>{t['name']}</strong><br/>
            <span style="font-size:12px;color:#555;">{t['trip_id']}</span>
          </td>
          <td style="padding:10px 14px;border-bottom:1px solid #eee;font-size:14px;font-weight:700;color:#0066cc;">
            ${price:,.0f}/person{pct_str}
          </td>
          <td style="padding:10px 14px;border-bottom:1px solid #eee;font-size:13px;">
            {airline} &middot; {date}
          </td>
          <td style="padding:10px 14px;border-bottom:1px solid #eee;">
            <a href="{best.get('url','#')}" style="color:#0066cc;font-size:13px;">Book &rarr;</a>
          </td>
        </tr>""")

    body = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             background:#f4f4f4;margin:0;padding:20px;">
  <div style="max-width:680px;margin:0 auto;background:#fff;border-radius:8px;
              overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1);">
    <div style="background:#1a1a2e;color:#fff;padding:20px 24px;">
      <h1 style="margin:0;font-size:20px;">✈ Flight Price Alert</h1>
      <p style="margin:5px 0 0;color:#aaa;font-size:13px;">
        {run_time} UTC &mdash; {len(alert_trips)} trip(s) with price drops or threshold hits
      </p>
    </div>
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="background:#f0f0f0;">
          <th style="padding:8px 14px;text-align:left;font-size:12px;color:#666;font-weight:600;">Trip</th>
          <th style="padding:8px 14px;text-align:left;font-size:12px;color:#666;font-weight:600;">Price</th>
          <th style="padding:8px 14px;text-align:left;font-size:12px;color:#666;font-weight:600;">Details</th>
          <th style="padding:8px 14px;text-align:left;font-size:12px;color:#666;font-weight:600;"></th>
        </tr>
      </thead>
      <tbody>{body}</tbody>
    </table>
    <div style="padding:14px 24px;font-size:11px;color:#aaa;border-top:1px solid #eee;text-align:center;">
      Automated flight price scan &middot; first_CB travel agent
    </div>
  </div>
</body>
</html>"""


def _render_plain(alert_trips: list[dict], run_time: str) -> str:
    lines = [
        f"Flight Price Alert — {run_time} UTC — {len(alert_trips)} trip(s)",
        "=" * 60,
        "",
    ]
    for t in alert_trips:
        best = t.get("best") or {}
        price = best.get("price_per_person")
        pct = t.get("price_drop_pct")
        pct_str = _pct_str(pct)
        lines.append(f"Trip    : {t['name']}")
        lines.append(f"Price   : ${price:,.0f}/person{pct_str}")
        lines.append(f"Airline : {best.get('airline','')}  {best.get('outbound_date','')}")
        lines.append(f"Book    : {best.get('url','')}")
        lines.append("")
    return "\n".join(lines)


def send_alert(results: list[dict]) -> None:
    """
    Send alert email for trips where alert=True.
    Does nothing if no alerts or env vars missing (logs warning).
    """
    alert_trips = [r for r in results if r.get("alert")]
    if not alert_trips:
        logger.info("No price alerts — skipping email.")
        return

    email_user = os.environ.get("EMAIL_USER")
    email_pass = os.environ.get("EMAIL_PASSWORD")
    email_to   = os.environ.get("EMAIL_TO")

    if not all([email_user, email_pass, email_to]):
        logger.warning(
            "Email env vars not set (EMAIL_USER, EMAIL_PASSWORD, EMAIL_TO). "
            "Skipping alert notification."
        )
        return

    run_time = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    # Build subject from first alert
    first = alert_trips[0]
    best  = first.get("best") or {}
    price = best.get("price_per_person", 0)
    pct   = first.get("price_drop_pct")
    pct_str = _pct_str(pct)
    route = f"{first.get('origin','?')}→{first.get('destination','?')}"
    subject = f"✈ Price Drop: {route} now ${price:,.0f}/person{pct_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = email_user
    msg["To"]      = email_to

    msg.attach(MIMEText(_render_plain(alert_trips, run_time), "plain"))
    msg.attach(MIMEText(_render_html(alert_trips, run_time),  "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(email_user, email_pass)
        smtp.sendmail(email_user, email_to, msg.as_string())

    logger.info("Alert sent: %d trip(s) → %s", len(alert_trips), email_to)
