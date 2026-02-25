"""
Keyword, location, and seniority filter for job listings.

Reads the `search` section from companies.yaml and applies three rules:
  1. Title must match at least one include keyword (case-insensitive)
  2. Title must NOT contain any exclude keyword
  3. Location must match a configured city, contain "remote", or be blank/unspecified

Rule 3 is intentionally permissive: many TPM roles list no location (remote/unspecified),
so hard-failing on blank locations would produce false negatives.
"""


def build_filter(search_config: dict):
    """
    Build a compiled filter closure from the search config.

    Args:
        search_config: The `search` block from companies.yaml:
            {
                "keywords":         ["Technical Program Manager", "TPM", ...],
                "exclude_keywords": ["intern", "contractor"],
                "locations":        ["San Francisco", "Seattle", "Remote", ...],
                "seniority":        ["Senior", "Staff", "Principal", "Lead"]
            }

    Returns:
        matches(job: dict) -> bool
    """
    keywords    = [k.lower() for k in search_config.get("keywords", [])]
    excludes    = [k.lower() for k in search_config.get("exclude_keywords", [])]
    locations   = [l.lower() for l in search_config.get("locations", [])]

    def matches(job: dict) -> bool:
        title    = (job.get("title") or "").lower()
        location = (job.get("location") or "").lower().strip()

        # Rule 1: must contain at least one include keyword
        if not any(kw in title for kw in keywords):
            return False

        # Rule 2: must NOT contain any exclude keyword
        if any(ex in title for ex in excludes):
            return False

        # Rule 3: location check
        # Pass if blank (unspecified / likely remote)
        if not location:
            return True
        # Pass if explicitly remote
        if "remote" in location:
            return True
        # Pass if any configured location appears in the location string
        if any(loc in location for loc in locations):
            return True

        # Location specified but not in our list — skip
        return False

    return matches


def filter_jobs(jobs: list[dict], search_config: dict) -> list[dict]:
    """Filter a list of normalized job dicts using the search config."""
    matches = build_filter(search_config)
    return [j for j in jobs if matches(j)]
