"""
Job application submitter.

Supported ATS:
  - Lever  : POST /v0/postings/{uuid}/apply  (multipart form)
  - Others : Returns apply URL for manual application

Returns a result dict per job:
    {"job_id", "title", "company", "status": "applied"|"manual"|"error", "detail"}
"""

import logging
import os

import requests
import yaml

logger = logging.getLogger(__name__)

_ROOT        = os.path.join(os.path.dirname(__file__), "..")
_CONFIG_PATH = os.path.join(_ROOT, "applicant.yaml")

LEVER_APPLY  = "https://api.lever.co/v0/postings/{posting_id}/apply"
TIMEOUT      = 20


def _load_applicant() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)["applicant"]


def _resume_bytes(applicant: dict) -> tuple:
    """Return (filename, bytes) for the resume file."""
    path = os.path.join(_ROOT, applicant["resume"])
    if not os.path.exists(path):
        raise FileNotFoundError(f"Resume not found at: {path}")
    with open(path, "rb") as f:
        return os.path.basename(path), f.read()


def _apply_lever(posting_uuid: str, applicant: dict) -> dict:
    """Submit application to Lever via their public postings API."""
    filename, resume_data = _resume_bytes(applicant)

    data = {
        "name":           applicant["name"],
        "email":          applicant["email"],
        "phone":          applicant.get("phone", ""),
        "urls[LinkedIn]": applicant.get("linkedin", ""),
        "org":            applicant.get("location", ""),
    }
    cover = applicant.get("cover_letter", "").strip()
    if cover:
        data["comments"] = cover

    files = {
        "resume": (filename, resume_data, "application/pdf"),
    }

    resp = requests.post(
        LEVER_APPLY.format(posting_id=posting_uuid),
        data=data,
        files=files,
        timeout=TIMEOUT,
    )

    if resp.status_code in (200, 201):
        return {"status": "applied", "detail": "Submitted via Lever API"}
    else:
        try:
            msg = resp.json()
        except Exception:
            msg = resp.text[:200]
        return {"status": "error", "detail": f"Lever {resp.status_code}: {msg}"}


def apply_to_job(job: dict) -> dict:
    """
    Attempt to apply to a single job. Returns a result dict.
    """
    job_id  = job.get("id", "")
    title   = job.get("title", "")
    company = job.get("company", "")
    url     = job.get("url", "")

    try:
        applicant = _load_applicant()
    except Exception as exc:
        return {"job_id": job_id, "title": title, "company": company,
                "status": "error", "detail": f"Config error: {exc}"}

    # ── Lever ────────────────────────────────────────────────────────────────
    if job_id.startswith("lever::"):
        parts = job_id.split("::")
        if len(parts) == 3:
            posting_uuid = parts[2]
            try:
                result = _apply_lever(posting_uuid, applicant)
            except Exception as exc:
                result = {"status": "error", "detail": str(exc)}
            return {**result, "job_id": job_id, "title": title,
                    "company": company, "url": url}

    # ── Greenhouse / Workday / others — manual URL ───────────────────────────
    return {
        "job_id":   job_id,
        "title":    title,
        "company":  company,
        "url":      url,
        "status":   "manual",
        "detail":   "ATS does not support API apply — use the link to apply manually.",
    }


def apply_to_jobs(jobs: list) -> list:
    """Apply to a list of jobs. Returns list of result dicts."""
    results = []
    for job in jobs:
        logger.info("Applying to: %s @ %s", job.get("title"), job.get("company"))
        result = apply_to_job(job)
        logger.info("  → %s: %s", result["status"], result["detail"])
        results.append(result)
    return results
