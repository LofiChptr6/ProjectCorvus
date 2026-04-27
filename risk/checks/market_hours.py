from datetime import datetime, time
import pytz

from risk.models import OrderRequest, AccountState, RiskResult, ALLOWED

_ET = pytz.timezone("America/New_York")
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)
_TRADING_DAYS = {0, 1, 2, 3, 4}  # Mon–Fri


def check(order: OrderRequest, account: AccountState, cfg: dict) -> RiskResult:
    if cfg.get("risk", {}).get("allow_extended_hours", False):
        return ALLOWED

    now_et = datetime.now(_ET)
    if now_et.weekday() not in _TRADING_DAYS:
        return RiskResult(
            allowed=False,
            reason=f"Market is closed (weekend). Current ET time: {now_et.strftime('%A %H:%M')}",
            check_name="market_hours",
        )
    current_time = now_et.time()
    if not (_RTH_OPEN <= current_time < _RTH_CLOSE):
        return RiskResult(
            allowed=False,
            reason=f"Outside regular trading hours (9:30–16:00 ET). Current ET: {now_et.strftime('%H:%M')}",
            check_name="market_hours",
        )
    return ALLOWED
