"""
Fetches job listings from the Lever public postings API.

Endpoint: GET https://api.lever.co/v0/postings/{lever_id}?mode=json
No authentication required. Returns a flat JSON array (not wrapped in an object).

Response shape:
    [{"id": "uuid", "text": "title", "categories": {"location": "...", "team": "..."},
      "hostedUrl": "...", "createdAt": 1234567890000}]

Note: createdAt is Unix milliseconds, not seconds.
"""

import requests
from datetime import datetime, timezone

LEVER_BASE = "https://api.lever.co/v0/postings/{lever_id}?mode=json"
REQUEST_TIMEOUT = 30


def fetch_lever_jobs(lever_id: str, company_name: str) -> list[dict]:
    """
    Fetch all open jobs for a company from Lever.

    Args:
        lever_id:     Posting slug (e.g. "palantir", "netflix", "mistral")
        company_name: Human-readable label used in output and email

    Returns:
        List of normalized job dicts with the same schema as greenhouse fetcher:
        {
            "id":        str,   # "lever::{lever_id}::{uuid}"
            "title":     str,
            "company":   str,
            "location":  str,
            "url":       str,
            "posted_at": str,   # ISO 8601
            "source":    "lever"
        }

    Raises:
        requests.HTTPError on 4xx/5xx
        requests.Timeout  on timeout
    """
    url = LEVER_BASE.format(lever_id=lever_id)
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    jobs = response.json()  # Lever returns a top-level array

    normalized = []
    for job in jobs:
        created_ms = job.get("createdAt") or 0
        posted_at = (
            datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat()
            if created_ms else ""
        )
        categories = job.get("categories") or {}
        location   = categories.get("location") or ""

        normalized.append({
            "id":        f"lever::{lever_id}::{job['id']}",
            "title":     job.get("text", ""),
            "company":   company_name,
            "location":  location,
            "url":       job.get("hostedUrl", ""),
            "posted_at": posted_at,
            "source":    "lever",
        })

    return normalized
