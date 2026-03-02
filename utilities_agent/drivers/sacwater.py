"""
Sacramento County Water — custom Playwright driver.

Platform: Angular SPA with Keycloak SSO login.
Login redirects to myprofile.saccounty.gov/realms/CoS/...

Selectors verified 2026-03-01:
  Login (SSO page):
    #username, #password, #kc-login
  Dashboard:
    .billing-container          — post-login check
    .billing-content .billing-amount  — current bill amount ($153.85)
    p.CUBSNotificationBlue      — due date notification text
    .billing-content div:nth-child(2) span  — bill period (1/22/2026 - 3/21/2026)
  Payment (/paynow):
    #ddlSelectAccount           — account selector
    #optPaymentTrue             — "Pay Total Amount Due" radio
    button.me-2                 — Review button
    #modal-basic-title          — confirm modal title (wait for modal)
    locator("Submit")           — Submit button in modal (text-matched)
    locator("Back")             — Back/cancel in modal

Credentials env vars: SACWATER_USERNAME, SACWATER_PASSWORD
"""

import logging
import os

from .base import BillInfo, PaymentResult, UtilityDriver

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15_000
PAYNOW_URL      = "https://myutilities.saccounty.gov/paynow"


class SacWaterDriver(UtilityDriver):

    def __init__(self, config: dict, credentials: dict, headless: bool = True):
        if os.environ.get("HEADLESS", "").lower() == "false":
            headless = False
        super().__init__(config, credentials, headless)
        self._page      = None
        self._browser   = None
        self._playwright = None
        self._bill: BillInfo | None = None

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    def _start(self) -> None:
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        self._browser    = self._playwright.chromium.launch(headless=self.headless)
        self._page       = self._browser.new_page()
        self._page.set_default_timeout(DEFAULT_TIMEOUT)

    def _stop(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        finally:
            self._page = self._browser = self._playwright = None

    # ------------------------------------------------------------------
    # UtilityDriver interface
    # ------------------------------------------------------------------

    def login(self) -> None:
        if self._page is None:
            self._start()
        page = self._page

        logger.info("Navigating to Sacramento County Water portal")
        page.goto("https://myutilities.saccounty.gov", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # Keycloak SSO login page
        page.fill("#username", self.credentials.get("username", ""))
        page.fill("#password", self.credentials.get("password", ""))
        page.click("#kc-login")

        # Wait for SSO redirect + Angular dashboard to fully render.
        # GitHub Actions is slower — use 45s and networkidle to be safe.
        page.wait_for_load_state("networkidle", timeout=45_000)
        page.wait_for_selector(".billing-container", timeout=45_000)
        page.wait_for_timeout(1000)
        logger.info("Login successful for %s", self.utility_name)

    def get_bill_info(self) -> BillInfo:
        page = self._page

        # Bill amount — current billing card (not prior period)
        page.wait_for_selector(".billing-content .billing-amount")
        amount_text = page.inner_text(".billing-content .billing-amount").strip()

        # Due date — notification banner (p.CUBSNotificationBlue is specific enough)
        try:
            due_text = page.inner_text("p.CUBSNotificationBlue").strip()
        except Exception:
            due_text = None

        # Bill period — second div > span inside .billing-content
        try:
            period_text = page.inner_text(
                ".billing-content div:nth-child(2) span"
            ).strip()
        except Exception:
            period_text = None

        # Parse amount
        import re
        cleaned = re.sub(r"[^\d.]", "", (amount_text or "").replace(",", ""))
        try:
            amount = float(cleaned)
        except (ValueError, TypeError):
            amount = None

        logger.info(
            "%s bill: %s due %r (period: %r)",
            self.utility_name, amount_text, due_text, period_text,
        )

        self._bill = BillInfo(
            amount=amount,
            due_date=due_text,
            bill_period=period_text,
        )
        return self._bill

    def pay_bill(self) -> PaymentResult:
        if self._bill is None:
            return PaymentResult(success=False, message="get_bill_info() must be called before pay_bill()")

        try:
            self._assert_cap(self._bill.amount or 0)
        except ValueError as exc:
            return PaymentResult(success=False, message=str(exc))

        page = self._page
        logger.info("Navigating to payment page for %s", self.utility_name)

        try:
            page.goto(PAYNOW_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            # Select account (index 1 = the real account, index 0 = "All Accounts")
            page.select_option("#ddlSelectAccount", index=1)
            page.wait_for_timeout(500)

            # Choose "Pay Total Amount Due"
            page.click("#optPaymentTrue")
            page.wait_for_timeout(500)

            # Click Review
            page.click("button.me-2")

            # Wait for confirm modal
            page.wait_for_selector("#modal-basic-title", timeout=10_000)
            page.wait_for_timeout(500)

            # Click Submit (text-matched locator to distinguish from Back)
            page.locator("button", has_text="Submit").click()

            # Wait for modal to close (success = modal disappears or URL changes)
            page.wait_for_selector(
                "#modal-basic-title",
                state="hidden",
                timeout=30_000,
            )

            # Allow Angular to re-render
            page.wait_for_timeout(2000)

            logger.info("Payment submitted for %s", self.utility_name)
            return PaymentResult(
                success=True,
                message=(
                    f"Payment of ${self._bill.amount:.2f} submitted for {self.utility_name}. "
                    "Allow 3 business days to process."
                ),
            )

        except Exception as exc:
            logger.error("Payment failed for %s: %s", self.utility_name, exc)
            return PaymentResult(success=False, message=f"Payment failed: {exc}")
        finally:
            self._stop()

    def check_only(self) -> BillInfo:
        try:
            self.login()
            return self.get_bill_info()
        finally:
            self._stop()
