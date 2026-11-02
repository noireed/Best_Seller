from datetime import datetime, time
import math
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


class RiskManager:
    def __init__(self, settings):
        self.s = settings

    def check_order(self, side, symbol, quantity, price, portfolio, limits, now=None):
        now = (now or datetime.now(KST)).astimezone(KST)
        if now.weekday() >= 5 or not (time(9, 0) <= now.time() < time(15, 0)):
            return False, "거래 가능 평일·시간 외 (공휴일은 서버 판정)"
        try:
            def number(value):
                if isinstance(value, bool):
                    raise ValueError("boolean")
                result = float(value)
                if not math.isfinite(result) or result < 0:
                    raise ValueError("invalid number")
                return result
            if side not in ("BUY", "SELL") or not isinstance(quantity, int) or isinstance(quantity, bool):
                raise ValueError("invalid order")
            if quantity <= 0 or number(price) <= 0:
                raise ValueError("invalid quantity or price")
            total, cash = number(portfolio["totalAssetValue"]), number(portfolio["availableCash"])
            if total <= 0 or cash > total:
                raise ValueError("invalid asset state")
            holdings = portfolio["holdings"]
            if not isinstance(holdings, list):
                raise ValueError("invalid holdings")
            remaining = number(limits["remainingTrades"])
            cooldowns = limits["cooldowns"]
            if not isinstance(cooldowns, list):
                raise ValueError("invalid cooldowns")
            if remaining <= 0:
                return False, "시간당 거래 횟수 소진"
            for row in cooldowns:
                if row["symbol"] == symbol:
                    at = datetime.fromisoformat(row["tradableAt"].replace("Z", "+00:00"))
                    if at.tzinfo is None:
                        raise ValueError("timezone required")
                    if at > now:
                        return False, "종목 쿨다운"
            holding = next((h for h in holdings if h["symbol"] == symbol), None)
            held = number(holding["quantity"]) if holding else 0
            available = number(holding["availableQuantity"]) if holding else 0
            if available > held:
                raise ValueError("invalid available quantity")
            amount = quantity * price
            if amount > self.s.max_order_value_krw:
                return False, "단일 주문 최대 금액 초과"
            if side == "BUY":
                cost = amount + math.floor(amount * .00015)
                if cost > cash:
                    return False, "가용 현금 부족"
                if held * price + amount > total * self.s.max_position_pct:
                    return False, "종목 최대 비중 초과"
                if cash - cost < total * self.s.cash_reserve_pct:
                    return False, "최소 현금 비율 위반"
            elif quantity > available:
                return False, "가용 주식 부족"
        except (KeyError, TypeError, ValueError, OverflowError):
            return False, "필수 위험 입력 누락·비정상: 주문 차단"
        return True, "OK"
