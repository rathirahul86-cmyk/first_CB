"""
Workday job fetcher — STUB (Phase 2).

Workday does not expose a public REST API. Each company hosts its own
Workday instance. To implement this fetcher:

  1. Install Playwright:
         pip install playwright && playwright install chromium

  2. For each company, identify the Workday search URL, e.g.:
         https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
         https://apple.wd5.myworkdayjobs.com/en-US/careers

  3. Use async_playwright to load the page, wait for job cards, extract data:

         from playwright.sync_api import sync_playwright

         def fetch_workday_jobs(career_url, company_name, search_term="TPM"):
             with sync_playwright() as p:
                 browser = p.chromium.launch(headless=True)
                 page = browser.new_page()
                 page.goto(career_url)
                 page.wait_for_selector("[data-automation-id='jobTitle']", timeout=15000)
                 cards = page.query_selector_all("[data-automation-id='jobTitle']")
                 jobs = []
                 for card in cards:
                     title = card.inner_text()
                     url   = card.get_attribute("href")
                     jobs.append({"title": title, "url": url, ...})
                 browser.close()
             return jobs

  4. Wire it into scanner.py fetch_company_jobs() where ats == "workday".

Companies currently skipped (8):
    NVIDIA, Apple, Alphabet (Google), Microsoft, Meta, Tesla, IBM, Adobe
"""

import logging

logger = logging.getLogger(__name__)


def fetch_workday_jobs(workday_id: str, career_url: str, company_name: str) -> list[dict]:
    """Placeholder — returns empty list until Playwright is implemented."""
    logger.warning(
        "Workday fetcher not yet implemented — skipping %s. "
        "See fetchers/workday.py for implementation guide.",
        company_name,
    )
    return []
