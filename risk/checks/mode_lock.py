from risk.models import OrderRequest, AccountState, RiskResult, ALLOWED
from ibkr.client import get_mode


def check(order: OrderRequest, account: AccountState, cfg: dict) -> RiskResult:
    expected = cfg.get("trading", {}).get("mode", "paper")
    actual = get_mode()
    if expected != actual:
        return RiskResult(
            allowed=False,
            reason=f"Mode mismatch: config says '{expected}' but IBKR connection is '{actual}'.",
            check_name="mode_lock",
        )
    return ALLOWED
