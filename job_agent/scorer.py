"""
Resume-to-job keyword overlap scorer.

Approach: weighted keyword groups tuned to Rahul Rathi's resume profile.
Each group contributes its weight once if ANY keyword in the group appears
in the job text (title + description). Score is normalized to 0-100.

Calibration reference: Waymo TPM (simulation compute) JD closely matches
Rahul's ML platform / GPU capacity / infrastructure background.

No external API keys required — uses only public ATS description endpoints.
"""

import html as html_lib
import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15  # seconds

# ---------------------------------------------------------------------------
# Weighted keyword taxonomy
# Each entry: ([keyword_variants], weight)
# Weight reflects how strongly the skill appears on Rahul's resume AND how
# differentiated it is (generic words like "agile" score low).
# ---------------------------------------------------------------------------
KEYWORD_GROUPS = [
    # ── Tier 1: Core identity (10 pts each) ─────────────────────────────────
    (["ml platform", "mlops", "machine learning platform", "ml infrastructure",
      "ml lifecycle", "model lifecycle"], 10),
    (["gpu", "gpu cluster", "gpu utilization", "gpu scheduling",
      "gpu infrastructure", "gpu compute"], 10),
    (["model training", "training infrastructure", "training pipeline",
      "pre-training", "fine-tuning", "model development"], 10),

    # ── Tier 2: Highly aligned (9 pts) ──────────────────────────────────────
    (["inference", "model inference", "inference infrastructure",
      "model serving", "model deployment", "production deployment"], 9),
    (["generative ai", "gen ai", "llm", "large language model",
      "foundation model", "foundational model", "frontier model",
      "transformer", "multimodal"], 9),
    (["compute capacity", "capacity planning", "capacity management",
      "resource allocation", "resource management", "workload scheduling",
      "compute resources", "hpc scheduling"], 9),

    # ── Tier 3: Technical platform (8 pts) ───────────────────────────────────
    (["kubernetes", "k8s", "slurm", "ray", "high performance computing",
      "hpc", "distributed computing", "distributed training"], 8),
    (["data platform", "data infrastructure", "data pipeline",
      "data lake", "data engineering", "data systems"], 8),
    (["observability", "telemetry", "utilization metrics", "system metrics",
      "monitoring", "dashboards", "performance tracking"], 8),

    # ── Tier 4: Program management (7 pts) ───────────────────────────────────
    (["technical program manager", "tpm", "senior tpm", "staff tpm",
      "principal tpm", "lead tpm"], 7),
    (["cross-functional", "stakeholder", "program execution",
      "delivery milestones", "roadmap", "program structure",
      "influence without authority", "dependencies"], 7),

    # ── Tier 5: Complementary skills (5–6 pts) ───────────────────────────────
    (["data quality", "data lineage", "data annotation", "data labeling",
      "data collection", "ground truth", "dataset"], 6),
    (["simulation", "synthetic data", "robotic data",
      "simulated environment"], 6),
    (["python", "sql"], 5),
    (["computer vision", "perception", "image sensor", "object detection",
      "tracking", "spatial tracking"], 5),
    (["cloud", "aws", "azure", "gcp", "cloud infrastructure",
      "cloud platform"], 5),

    # ── Tier 6: Bonus domain (3–4 pts) ───────────────────────────────────────
    (["ar/vr", "augmented reality", "virtual reality",
      "quest", "reality labs", "metaverse", "mixed reality"], 4),
    (["hardware", "silicon", "chip", "semiconductor", "hw/sw",
      "hw bring-up", "firmware", "bring-up"], 4),
    (["autonomous vehicle", "autonomous driving", "self-driving",
      "waymo driver", "robotics"], 4),
    (["agile", "scrum", "okr", "milestone", "sprint"], 3),
    (["supply chain", "operations", "vendor management"], 3),
]

_MAX_SCORE: int = sum(w for _, w in KEYWORD_GROUPS)


# ---------------------------------------------------------------------------
# Description fetchers (best-effort — errors return empty string)
# ---------------------------------------------------------------------------

def _fetch_greenhouse_description(board_id: str, job_id: str) -> str:
    """Fetch full job description HTML from Greenhouse boards API."""
    try:
        url = f"https://boards-api.greenhouse.io/v1/boards/{board_id}/jobs/{job_id}"
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("content") or ""
    except Exception as exc:
        logger.debug("Greenhouse description fetch failed (%s/%s): %s", board_id, job_id, exc)
        return ""


def _get_description(job: dict) -> str:
    """
    Return the full description text for a job.
    - Lever jobs carry description fields in the listing response.
    - Greenhouse jobs need a second API call to /boards/{id}/jobs/{job_id}.
    """
    # Lever / pre-fetched descriptions
    desc = job.get("description") or job.get("descriptionPlain") or ""
    if desc:
        return desc

    # Greenhouse: parse board_id and numeric job ID from our composite key
    job_id_str = job.get("id", "")
    if job_id_str.startswith("greenhouse::"):
        parts = job_id_str.split("::")
        if len(parts) == 3:
            board_id, numeric_id = parts[1], parts[2]
            return _fetch_greenhouse_description(board_id, numeric_id)

    return ""


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Unescape HTML entities, strip HTML tags, and collapse whitespace."""
    text = html_lib.unescape(text)       # &lt; → <, &amp; → &, etc.
    text = re.sub(r"<[^>]+>", " ", text) # strip <tags>
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def score_job(job: dict) -> int:
    """
    Score a single job 0-100 against Rahul's resume profile.
    Fetches description from ATS API (Greenhouse only; Lever is inline).
    """
    raw_text = " ".join([
        job.get("title") or "",
        job.get("company") or "",
        _get_description(job),
    ])
    text = _clean(raw_text)

    earned = 0
    for keywords, weight in KEYWORD_GROUPS:
        if any(kw in text for kw in keywords):
            earned += weight

    return round((earned / _MAX_SCORE) * 100)


def score_jobs(jobs: list) -> list:
    """
    Add match_score (0-100) to each job dict and return sorted
    by match_score descending (best match first).
    """
    scored = []
    for job in jobs:
        try:
            s = score_job(job)
        except Exception as exc:
            logger.warning("Scoring failed for %s: %s", job.get("id"), exc)
            s = 0
        scored.append({**job, "match_score": s})

    return sorted(scored, key=lambda j: j["match_score"], reverse=True)
