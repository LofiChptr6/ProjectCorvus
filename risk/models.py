"""Data models for the risk layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OrderRequest:
    symbol: str
    action: str          # "BUY" or "SELL"
    quantity: float
    order_type: str      # "MKT", "LMT", "STP"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    reasoning: Optional[str] = None
    agent_name: str = ""
    session_id: Optional[str] = None
    current_mark: Optional[float] = None  # live quote, set by callers that have one

    @property
    def effective_price(self) -> Optional[float]:
        return self.limit_price or self.stop_price or self.current_mark


@dataclass
class AccountState:
    nav: float
    cash: float
    buying_power: float
    realized_pnl_today: float
    positions: list[dict] = field(default_factory=list)


@dataclass
class RiskResult:
    allowed: bool
    reason: str = ""
    check_name: str = ""
    # When True alongside allowed=False, the order is blocked by default but
    # the caller MAY ask the user via Telegram to override (used by the sub-10
    # share gate on expensive tickers).
    needs_telegram_approval: bool = False


ALLOWED = RiskResult(allowed=True, reason="", check_name="")
