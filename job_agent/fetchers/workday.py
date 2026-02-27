"""
Workday job fetcher using Workday's internal CXS (Career Experience Service) API.

Most Workday-hosted career sites expose a POST endpoint that powers their job board.
This is not a documented public API, but it is unauthenticated for companies that
don't restrict external access.

Verified working (as of 2026-02):
    NVIDIA:  nvidia.wd5.myworkdayjobs.com / NVIDIAExternalCareerSite
    Adobe:   adobe.wd5.myworkdayjobs.com  / external_experienced

Not accessible (return 422 or require auth):
    Microsoft, Meta, Google, Tesla, IBM, Apple — these use custom career portals
    or have blocked external Workday API access. Apple in particular uses a
    JavaScript-only search portal (jobs.apple.com) with no public API.

To add a new Workday company:
    1. Open the company's career site, click any job, inspect the URL:
           https://{workday_id}.wd5.myworkdayjobs.com/en-US/{workday_board}/job/...
    2. Test the CXS endpoint manually:
           curl -X POST https://{id}.wd5.myworkdayjobs.com/wday/cxs/{id}/{board}/jobs
                -H 'Content-Type: application/json'
                -d '{"appliedFacets":{},"limit":5,"offset":0,"searchText":""}'
    3. If it returns 200 with jobPostings, add workday_id + workday_board to companies.yaml.
    4. If it returns 422, the company blocks external API access — mark as custom.
"""

import logging
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20  # seconds; Workday can be slow
_PAGE_SIZE = 20       # Workday CXS API max; requests with limit>20 return 400


def fetch_workday_jobs(
    workday_id: str,
    workday_board: str,
    career_url: str,
    company_name: str,
    search_terms: Optional[list] = None,
) -> list[dict]:
    """
    Fetch jobs from a Workday-hosted career site via the CXS API.

    Uses server-side keyword search (searchText) for each configured keyword to
    avoid paginating through thousands of total jobs. Deduplicates results by
    externalPath across all search terms.

    Args:
        workday_id:    Workday tenant identifier (e.g., "nvidia")
        workday_board: Board/job-site name within the tenant (e.g., "NVIDIAExternalCareerSite")
        career_url:    Human-readable career page URL (unused, kept for interface compatibility)
        company_name:  Display name for logging
        search_terms:  Keywords to search. Defaults to TPM/EPM variants if None.

    Returns:
        List of normalized job dicts. Returns [] if the API is inaccessible.
    """
    if not workday_board:
        logger.warning(
            "Workday board not configured for %s — skipping. "
            "Add workday_board to companies.yaml to enable.",
            company_name,
        )
        return []

    if search_terms is None:
        # Use focused terms to avoid fetching thousands of irrelevant jobs.
        # "Technical Program Manager" covers TPM, Senior TPM, Staff TPM, etc.
        # "Engineering Program Manager" covers EPM variants (common at Apple, NVIDIA).
        search_terms = [
            "Technical Program Manager",
            "Engineering Program Manager",
        ]

    api_url = (
        f"https://{workday_id}.wd5.myworkdayjobs.com"
        f"/wday/cxs/{workday_id}/{workday_board}/jobs"
    )
    req_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

    seen_paths: set = set()
    all_jobs: list[dict] = []

    for term in search_terms:
        offset = 0
        while True:
            payload = {
                "appliedFacets": {},
                "limit": _PAGE_SIZE,
                "offset": offset,
                "searchText": term,
            }
            try:
                resp = requests.post(api_url, json=payload, headers=req_headers, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as exc:
                logger.warning("[%s] Workday request failed for term '%s': %s", company_name, term, exc)
                break

            if resp.status_code == 422:
                logger.warning(
                    "[%s] Workday API returned 422 — company may block external access. "
                    "Mark as ats: custom in companies.yaml.",
                    company_name,
                )
                return []  # entire company is blocked

            if resp.status_code != 200:
                logger.error("[%s] Workday API returned %d for term '%s'", company_name, resp.status_code, term)
                break

            postings = resp.json().get("jobPostings", [])
            for job in postings:
                path = job.get("externalPath", "")
                if path and path not in seen_paths:
                    seen_paths.add(path)
                    all_jobs.append(job)

            if len(postings) < _PAGE_SIZE:
                break  # last page for this term
            offset += _PAGE_SIZE

    return [_normalize(job, workday_id, workday_board, company_name) for job in all_jobs]


def _normalize(job: dict, workday_id: str, workday_board: str, company_name: str) -> dict:
    """Normalize a Workday job posting to the shared schema."""
    external_path = job.get("externalPath") or ""
    # Strip leading slash if present
    if external_path.startswith("/"):
        external_path = external_path[1:]

    job_url = (
        f"https://{workday_id}.wd5.myworkdayjobs.com/en-US/{workday_board}/job/{external_path}"
        if external_path
        else f"https://{workday_id}.wd5.myworkdayjobs.com/en-US/{workday_board}"
    )

    # Workday uses a path-based unique identifier
    job_id = external_path.split("/")[-1] if external_path else job.get("bulletFields", ["unknown"])[0]

    return {
        "id":       f"workday::{workday_id}::{job_id}",
        "title":    job.get("title", ""),
        "company":  company_name,
        "location": job.get("locationsText") or "",
        "url":      job_url,
        "source":   "workday",
    }
