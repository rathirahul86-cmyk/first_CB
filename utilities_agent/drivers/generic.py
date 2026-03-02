"""
Generic YAML-configured Playwright driver.

Executes step sequences defined in utilities.yaml. When no steps are defined,
uses a default sequence that covers: login → scrape bill → pay.

Supported step actions:
  navigate    — page.goto(url)
  fill        — page.fill(selector, value)
  click       — page.click(selector)
  wait_for    — page.wait_for_selector(selector, timeout=ms)
  extract_text — grab inner_text, store in result dict under key 'as'

Environment variable HEADLESS=false overrides the headless constructor arg.
"""

import logging
import os
import re
from typing import Optional

from .base import BillInfo, PaymentResult, UtilityDriver

logger = logging.getLogger(__name__)

# Default wait timeout for wait_for steps (ms)
DEFAULT_TIMEOUT = 15_000


def _parse_amount(text: str) -> Optional[float]:
    """Strip currency symbols/commas and parse to float. Returns None on failure."""
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


class GenericDriver(UtilityDriver):
    """
    Step-based Playwright driver driven by YAML configuration.

    If the utility config has a top-level 'steps' key, those steps are used.
    Otherwise the default login/bill/pay sequences are constructed from 'selectors'.
    """

    def __init__(self, config: dict, credentials: dict, headless: bool = True):
        # Allow env var override for debugging
        if os.environ.get("HEADLESS", "").lower() == "false":
            headless = False
        super().__init__(config, credentials, headless)
        self._page = None
        self._browser = None
        self._playwright = None
        self._scraped: dict = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _start_browser(self) -> None:
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._page = self._browser.new_page()
        self._page.set_default_timeout(DEFAULT_TIMEOUT)

    def _stop_browser(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        finally:
            self._page = None
            self._browser = None
            self._playwright = None

    # ------------------------------------------------------------------
    # Step execution engine
    # ------------------------------------------------------------------

    def _sel(self, key: str) -> str:
        """Look up a selector by name from config['selectors']."""
        selectors = self.config.get("selectors", {})
        if key not in selectors:
            raise KeyError(
                f"Selector '{key}' not found in utilities.yaml for {self.utility_id}. "
                "Run with headless=False to inspect the page and add the selector."
            )
        return selectors[key]

    def _run_step(self, step: dict) -> None:
        """Execute a single step dict."""
        action = step["action"]
        page = self._page

        if action == "navigate":
            url = step.get("url") or self.config.get("login_url")
            logger.debug("navigate → %s", url)
            page.goto(url, wait_until="domcontentloaded")

        elif action == "fill":
            selector = step.get("selector") or self._sel(step["selector_key"])
            value = step.get("value") or self.credentials.get(step.get("credential_key", ""))
            logger.debug("fill %s", selector)
            page.fill(selector, value)

        elif action == "click":
            selector = step.get("selector") or self._sel(step["selector_key"])
            logger.debug("click %s", selector)
            page.click(selector)

        elif action == "wait_for":
            selector = step.get("selector") or self._sel(step["selector_key"])
            timeout = step.get("timeout", DEFAULT_TIMEOUT)
            logger.debug("wait_for %s (timeout=%dms)", selector, timeout)
            page.wait_for_selector(selector, timeout=timeout)

        elif action == "extract_text":
            selector = step.get("selector") or self._sel(step["selector_key"])
            key = step.get("as", step.get("selector_key", "text"))
            logger.debug("extract_text %s → %s", selector, key)
            try:
                self._scraped[key] = page.inner_text(selector).strip()
            except Exception as exc:
                logger.warning("extract_text failed for %s: %s", selector, exc)
                self._scraped[key] = None

        else:
            logger.warning("Unknown step action '%s' — skipping", action)

    def _run_steps(self, steps: list) -> None:
        for step in steps:
            self._run_step(step)

    # ------------------------------------------------------------------
    # Default step sequences built from 'selectors'
    # ------------------------------------------------------------------

    def _default_login_steps(self) -> list:
        return [
            {"action": "navigate", "url": self.config["login_url"]},
            {"action": "fill",     "selector": self._sel("username_field"),
             "value": self.credentials.get("username", "")},
            {"action": "fill",     "selector": self._sel("password_field"),
             "value": self.credentials.get("password", "")},
            {"action": "click",    "selector": self._sel("login_button")},
            {"action": "wait_for", "selector": self._sel("post_login_check")},
        ]

    def _default_bill_steps(self) -> list:
        steps = [
            {"action": "wait_for",    "selector": self._sel("bill_amount")},
            {"action": "extract_text","selector": self._sel("bill_amount"),    "as": "amount"},
        ]
        for key in ("due_date", "bill_period"):
            if key in self.config.get("selectors", {}):
                steps.append({
                    "action": "extract_text",
                    "selector": self._sel(key),
                    "as": key,
                })
        return steps

    def _default_pay_steps(self) -> list:
        steps = []
        sel = self.config.get("selectors", {})
        if "pay_nav_link" in sel:
            steps.append({"action": "click",    "selector": self._sel("pay_nav_link")})
        steps += [
            {"action": "wait_for", "selector": self._sel("payment_submit")},
            {"action": "click",    "selector": self._sel("payment_submit")},
        ]
        if "confirm_button" in sel:
            steps += [
                {"action": "wait_for", "selector": self._sel("confirm_button")},
                {"action": "click",    "selector": self._sel("confirm_button")},
            ]
        steps.append({"action": "wait_for", "selector": self._sel("success_indicator")})
        return steps

    # ------------------------------------------------------------------
    # UtilityDriver interface
    # ------------------------------------------------------------------

    def login(self) -> None:
        if self._page is None:
            self._start_browser()
        custom_steps = self.config.get("steps", {}).get("login")
        steps = custom_steps if custom_steps else self._default_login_steps()
        logger.info("Logging in to %s", self.utility_name)
        self._run_steps(steps)
        logger.info("Login successful for %s", self.utility_name)

    def get_bill_info(self) -> BillInfo:
        custom_steps = self.config.get("steps", {}).get("bill")
        steps = custom_steps if custom_steps else self._default_bill_steps()
        logger.info("Scraping bill info for %s", self.utility_name)
        self._run_steps(steps)

        raw_amount = self._scraped.get("amount")
        amount = _parse_amount(raw_amount) if raw_amount else None
        due_date = self._scraped.get("due_date")
        bill_period = self._scraped.get("bill_period")

        logger.info(
            "%s bill: $%s due %s (period: %s)",
            self.utility_name, amount, due_date, bill_period,
        )
        return BillInfo(
            amount=amount,
            due_date=due_date,
            bill_period=bill_period,
            raw=dict(self._scraped),
        )

    def pay_bill(self) -> PaymentResult:
        amount = self._scraped.get("amount")
        parsed = _parse_amount(amount) if amount else None

        if parsed is None:
            return PaymentResult(success=False, message="Could not determine bill amount — payment aborted.")

        try:
            self._assert_cap(parsed)
        except ValueError as exc:
            return PaymentResult(success=False, message=str(exc))

        custom_steps = self.config.get("steps", {}).get("pay")
        steps = custom_steps if custom_steps else self._default_pay_steps()

        logger.info("Executing payment of $%.2f for %s", parsed, self.utility_name)
        try:
            self._run_steps(steps)
            self._stop_browser()
            return PaymentResult(
                success=True,
                message=f"Payment of ${parsed:.2f} submitted successfully for {self.utility_name}.",
            )
        except Exception as exc:
            self._stop_browser()
            logger.error("Payment failed for %s: %s", self.utility_name, exc)
            return PaymentResult(success=False, message=f"Payment failed: {exc}")

    def check_only(self) -> BillInfo:
        """Login and scrape bill without paying. Closes browser when done."""
        try:
            self.login()
            return self.get_bill_info()
        finally:
            self._stop_browser()
