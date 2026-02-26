"""
Outlook SMTP email notifier.

Sends an HTML digest (with plain-text fallback) listing all new TPM jobs
found in the current run. Sends nothing if new_jobs is empty.

Required environment variables:
    EMAIL_USER      Your Outlook address (sender), e.g. rathir1@outlook.com
    EMAIL_PASSWORD  Your Outlook account password (or app password if MFA enabled)
    EMAIL_TO        Recipient address

SMTP: smtp-mail.outlook.com:587 with STARTTLS (personal @outlook.com / @hotmail.com)
"""

import os
import logging
import smtplib
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def _group_by_company(jobs: list[dict]) -> dict[str, list[dict]]:
    grouped = defaultdict(list)
    for job in jobs:
        grouped[job["company"]].append(job)
    return dict(sorted(grouped.items()))


def _render_html(jobs: list[dict], run_time: str) -> str:
    grouped = _group_by_company(jobs)

    rows = []
    for company, company_jobs in grouped.items():
        rows.append(f"""
        <tr>
          <td colspan="3" style="background:#1a1a2e;color:#e0e0e0;font-weight:bold;
              padding:8px 14px;font-size:13px;letter-spacing:.4px;">
            {company} &mdash; {len(company_jobs)} role(s)
          </td>
        </tr>""")
        for j in company_jobs:
            loc = j.get("location") or "Remote / Unspecified"
            rows.append(f"""
        <tr>
          <td style="padding:7px 14px;border-bottom:1px solid #eee;font-size:13px;">
            <a href="{j['url']}" style="color:#0066cc;text-decoration:none;">{j['title']}</a>
          </td>
          <td style="padding:7px 14px;border-bottom:1px solid #eee;font-size:12px;color:#555;">
            {loc}
          </td>
          <td style="padding:7px 14px;border-bottom:1px solid #eee;font-size:12px;">
            <a href="{j['url']}" style="color:#0066cc;">Apply &rarr;</a>
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
      <h1 style="margin:0;font-size:20px;">TPM Job Digest</h1>
      <p style="margin:5px 0 0;color:#aaa;font-size:13px;">
        {run_time} UTC &mdash; {len(jobs)} new role(s)
      </p>
    </div>
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="background:#f0f0f0;">
          <th style="padding:8px 14px;text-align:left;font-size:12px;color:#666;font-weight:600;">Title</th>
          <th style="padding:8px 14px;text-align:left;font-size:12px;color:#666;font-weight:600;">Location</th>
          <th style="padding:8px 14px;text-align:left;font-size:12px;color:#666;font-weight:600;"></th>
        </tr>
      </thead>
      <tbody>
        {body}
      </tbody>
    </table>
    <div style="padding:14px 24px;font-size:11px;color:#aaa;border-top:1px solid #eee;text-align:center;">
      Automated TPM job scan &middot;
      <a href="https://github.com/rathirahul86-cmyk/first_CB" style="color:#aaa;">
        rathirahul86-cmyk/first_CB
      </a>
    </div>
  </div>
</body>
</html>"""


def _render_plain(jobs: list[dict], run_time: str) -> str:
    grouped = _group_by_company(jobs)
    lines = [
        f"TPM Job Digest — {run_time} UTC — {len(jobs)} new role(s)",
        "=" * 60,
        "",
    ]
    for company, company_jobs in grouped.items():
        lines.append(f"[{company}] — {len(company_jobs)} role(s)")
        for j in company_jobs:
            lines.append(f"  {j['title']}")
            lines.append(f"  Location : {j.get('location') or 'Remote / Unspecified'}")
            lines.append(f"  Apply    : {j['url']}")
            lines.append("")
        lines.append("")
    return "\n".join(lines)


def send_digest(new_jobs: list[dict]) -> None:
    """
    Send an HTML+plain-text digest email for new_jobs.
    Does nothing if new_jobs is empty.

    Raises:
        EnvironmentError   if required env vars are missing
        smtplib.SMTPException on connection or auth failure
    """
    if not new_jobs:
        logger.info("No new jobs — skipping email.")
        return

    email_user = os.environ.get("EMAIL_USER")
    email_pass = os.environ.get("EMAIL_PASSWORD")
    email_to   = os.environ.get("EMAIL_TO")

    if not all([email_user, email_pass, email_to]):
        raise EnvironmentError(
            "Set EMAIL_USER, EMAIL_PASSWORD, and EMAIL_TO to enable email notifications."
        )

    run_time = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    subject  = f"TPM Jobs ({len(new_jobs)} new) — {run_time}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = email_user
    msg["To"]      = email_to

    # Plain text first (lower priority fallback), HTML second (preferred by clients)
    msg.attach(MIMEText(_render_plain(new_jobs, run_time), "plain"))
    msg.attach(MIMEText(_render_html(new_jobs, run_time),  "html"))

    # Outlook personal accounts: smtp-mail.outlook.com:587
    with smtplib.SMTP("smtp-mail.outlook.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(email_user, email_pass)
        smtp.sendmail(email_user, email_to, msg.as_string())

    logger.info("Digest sent: %d jobs → %s", len(new_jobs), email_to)
