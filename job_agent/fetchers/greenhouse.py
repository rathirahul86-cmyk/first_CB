"""
Fetches job listings from the Greenhouse public boards API.

Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{greenhouse_id}/jobs
No authentication required. Returns all open jobs in one call (no pagination).

Response shape:
    {"jobs": [{"id": 123, "title": "...", "location": {"name": "..."}, "absolute_url": "...", "updated_at": "..."}]}
"""

import requests

GREENHOUSE_BASE = "https://boards-api.greenhouse.io/v1/boards/{greenhouse_id}/jobs"
REQUEST_TIMEOUT = 15


def fetch_greenhouse_jobs(greenhouse_id: str, company_name: str) -> list[dict]:
    """
    Fetch all open jobs for a company from Greenhouse.

    Args:
        greenhouse_id: Board slug (e.g. "openai", "anthropic", "stripe")
        company_name:  Human-readable label used in output and email

    Returns:
        List of normalized job dicts:
        {
            "id":        str,   # "greenhouse::{board}::{job_id}" — globally unique
            "title":     str,
            "company":   str,
            "location":  str,
            "url":       str,
            "posted_at": str,   # ISO 8601
            "source":    "greenhouse"
        }

    Raises:
        requests.HTTPError on 4xx/5xx
        requests.Timeout  on timeout
    """
    url = GREENHOUSE_BASE.format(greenhouse_id=greenhouse_id)
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    jobs = response.json().get("jobs", [])

    return [
        {
            "id":        f"greenhouse::{greenhouse_id}::{job['id']}",
            "title":     job.get("title", ""),
            "company":   company_name,
            "location":  (job.get("location") or {}).get("name", "") or "",
            "url":       job.get("absolute_url", ""),
            "posted_at": job.get("updated_at", ""),
            "source":    "greenhouse",
        }
        for job in jobs
    ]
