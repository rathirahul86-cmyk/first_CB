"""
Abstract base class for utility bill drivers.

Each driver is responsible for:
  1. Logging in to the utility website
  2. Extracting the current bill information
  3. Executing the payment flow (gated by payment_cap)

Credentials are sourced from environment variables:
  {UTILITY_ID_UPPER}_USERNAME  e.g. PGE_USERNAME
  {UTILITY_ID_UPPER}_PASSWORD  e.g. PGE_PASSWORD
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BillInfo:
    amount: Optional[float]        # parsed dollar amount, e.g. 127.43
    due_date: Optional[str]        # human-readable, e.g. "Mar 15, 2026"
    bill_period: Optional[str]     # human-readable, e.g. "Feb 1 – Feb 28"
    raw: dict = field(default_factory=dict)  # raw scraped text keyed by field name


@dataclass
class PaymentResult:
    success: bool
    message: str


class UtilityDriver(ABC):
    """
    Base class for utility website drivers.

    Subclasses must implement login(), get_bill_info(), and pay_bill().
    """

    def __init__(self, config: dict, credentials: dict, headless: bool = True):
        """
        Args:
            config:      One utility entry from utilities.yaml
            credentials: {"username": ..., "password": ...}
            headless:    Run browser headlessly (set False for debugging)
        """
        self.config = config
        self.credentials = credentials
        self.headless = headless
        self.utility_id = config["id"]
        self.utility_name = config.get("name", config["id"])
        self.payment_cap = float(config.get("payment_cap", 500.0))

    @abstractmethod
    def login(self) -> None:
        """Navigate to login URL and authenticate. Raises on failure."""

    @abstractmethod
    def get_bill_info(self) -> BillInfo:
        """Extract bill amount, due date, and period from the dashboard."""

    @abstractmethod
    def pay_bill(self) -> PaymentResult:
        """Execute the payment flow. Must check payment_cap before submitting."""

    def check_only(self) -> BillInfo:
        """Convenience: login + get bill info without paying."""
        self.login()
        return self.get_bill_info()

    def _assert_cap(self, amount: float) -> None:
        """Raise ValueError if amount exceeds payment_cap."""
        if amount > self.payment_cap:
            raise ValueError(
                f"Bill amount ${amount:.2f} exceeds payment cap "
                f"${self.payment_cap:.2f} for {self.utility_name}. "
                "Update payment_cap in utilities.yaml to proceed."
            )
