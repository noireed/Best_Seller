from datetime import datetime, time
from zoneinfo import ZoneInfo
from agents.schema import Decision

KST = ZoneInfo("Asia/Seoul")

def validate_order(decision: Decision, *, cash: float, holdings: dict,
                   price: float, locked: set, remaining_orders: int,
                   portfolio_value: float, max_weight: float = 0.10,
                   now: datetime | None = None):
    now = (now or datetime.now(KST)).astimezone(KST)
    if not (time(9, 0) <= now.time() < time(15, 0)):
        return False, "outside trading hours"
    if decision.action == "HOLD" or decision.quantity <= 0:
        return False, "no order"
    if remaining_orders <= 0 or decision.symbol in locked:
        return False, "order limit or cooldown"
    if price <= 0:
        return False, "invalid price"
    if decision.action == "BUY":
        if decision.quantity * price > cash:
            return False, "insufficient cash"
        current = holdings.get(decision.symbol, 0) * price
        if current + decision.quantity * price > portfolio_value * max_weight:
            return False, "position cap"
    elif decision.quantity > holdings.get(decision.symbol, 0):
        return False, "insufficient shares"
    return True, "approved"
