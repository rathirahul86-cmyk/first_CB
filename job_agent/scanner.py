"""
Orchestrator for the TPM job scanning agent.

Flow:
  1. Load companies.yaml
  2. For each company dispatch to the correct fetcher (greenhouse / lever / workday)
  3. Filter all jobs through keyword + location rules
  4. Check each passing job against the SQLite dedup store
  5. Mark all new jobs as seen (in one transaction)
  6. Send email digest if any new jobs exist
  7. Return a summary dict for the CLI to print
"""

import json
import logging
import os
import yaml
from datetime import datetime, timezone
from typing import Optional

from .fetchers.greenhouse import fetch_greenhouse_jobs
from .fetchers.lever      import fetch_lever_jobs
from .fetchers.workday    import fetch_workday_jobs
from .db                  import init_db, is_seen, mark_seen_batch
from .filter              import filter_jobs
from .notifier            import send_digest
from .scorer              import score_jobs

_RESULTS_PATH = os.path.join(os.path.dirname(__file__), "results.json")


def _write_results(filtered: list, new_job_ids: set, errors: list, total_fetched: int) -> None:
    """Write current scan results to results.json for the dashboard to read."""
    logger.info("Scoring %d jobs against resume profile…", len(filtered))
    scored = score_jobs(filtered)

    jobs_out = []
    for j in scored:
        jobs_out.append({
            "id":          j["id"],
            "title":       j["title"],
            "company":     j["company"],
            "location":    j.get("location") or "",
            "url":         j["url"],
            "source":      j.get("source", ""),
            "is_new":      j["id"] in new_job_ids,
            "match_score": j.get("match_score", 0),
        })

    payload = {
        "last_scan":     datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total_fetched": total_fetched,
        "total_matched": len(filtered),
        "new_count":     len(new_job_ids),
        "errors":        errors,
        "jobs":          jobs_out,
    }
    with open(_RESULTS_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    logger.info("Results written to %s", _RESULTS_PATH)

logger = logging.getLogger(__name__)

# Default: companies.yaml lives one level above this package
_DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "..", "companies.yaml")


def load_config(path: Optional[str] = None) -> dict:
    with open(path or _DEFAULT_CONFIG) as f:
        return yaml.safe_load(f)


def _fetch(company: dict) -> tuple:
    """
    Dispatch to the right fetcher. Returns (jobs, error_message).
    error_message is None on success.
    """
    ats  = (company.get("ats") or "").lower()
    name = company.get("name", "Unknown")

    try:
        if ats == "greenhouse":
            jobs = fetch_greenhouse_jobs(company["greenhouse_id"], name)
        elif ats == "lever":
            jobs = fetch_lever_jobs(company["lever_id"], name)
        elif ats == "workday":
            jobs = fetch_workday_jobs(
                company.get("workday_id", ""),
                company.get("workday_board", ""),
                company.get("career_url", ""),
                name,
            )
        else:
            # amazon / taleo / custom — no fetcher yet
            logger.info("Skipping %s (ATS '%s' not implemented)", name, ats)
            return [], None

        logger.info("[%s] fetched %d jobs", name, len(jobs))
        return jobs, None

    except Exception as exc:
        msg = f"{name} ({ats}): {type(exc).__name__}: {exc}"
        logger.error("Error fetching %s: %s", name, exc)
        return [], msg


def run_scan(
    config_path: Optional[str] = None,
    db_path: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """
    Execute a full scan cycle.

    Args:
        config_path: Path to companies.yaml (default: auto-detected)
        db_path:     Path to SQLite DB (default: job_agent/jobs_seen.db)
        dry_run:     If True, skip DB writes and email. Useful for testing.

    Returns:
        {
            "total_fetched":  int,
            "total_filtered": int,
            "new_jobs":       int,
            "errors":         list[str],
            "jobs":           list[dict]   # only present in dry_run mode
        }
    """
    config    = load_config(config_path)
    companies = config.get("companies", [])
    search    = config.get("search", {})

    init_db(db_path)  # always init — idempotent, needed even in dry_run for is_seen()

    all_jobs = []
    errors   = []

    for company in companies:
        jobs, err = _fetch(company)
        if err:
            errors.append(err)
        all_jobs.extend(jobs)

    logger.info("Fetched %d total jobs from %d companies", len(all_jobs), len(companies))

    filtered = filter_jobs(all_jobs, search)
    logger.info("After filter: %d jobs match criteria", len(filtered))

    new_jobs    = [j for j in filtered if not is_seen(j["id"], db_path)]
    new_job_ids = {j["id"] for j in new_jobs}
    logger.info("New (unseen) jobs: %d", len(new_jobs))

    if dry_run:
        logger.info("[DRY RUN] Would notify %d jobs. No DB writes or email.", len(new_jobs))
        _write_results(filtered, new_job_ids, errors, len(all_jobs))
        return {
            "total_fetched":  len(all_jobs),
            "total_filtered": len(filtered),
            "new_jobs":       len(new_jobs),
            "errors":         errors,
            "jobs":           new_jobs,
        }

    if new_jobs:
        mark_seen_batch(new_jobs, db_path)

    # Write results.json for the dashboard
    _write_results(filtered, new_job_ids, errors, len(all_jobs))

    try:
        send_digest(new_jobs)
    except Exception as exc:
        errors.append(f"Email failed: {type(exc).__name__}: {exc}")
        logger.error("Email error: %s", exc)

    return {
        "total_fetched":  len(all_jobs),
        "total_filtered": len(filtered),
        "new_jobs":       len(new_jobs),
        "errors":         errors,
    }
