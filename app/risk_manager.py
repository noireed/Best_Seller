from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
KST = ZoneInfo("Asia/Seoul")

class RiskManager:
    def __init__(self, settings):
        self.s = settings

    def check_order(self, side, symbol, quantity, price, portfolio, limits):
        now = datetime.now(KST)
        if not (time(9, 0) <= now.time() < time(15, 0)):
            return False, "거래 가능 시간(09:00~15:00)이 아닙니다."
        if quantity <= 0 or price <= 0:
            return False, "수량과 가격은 양수여야 합니다."
        if quantity * price > self.s.max_order_value_krw:
            return False, "단일 주문 최대 금액 초과"
        remaining = limits.get("remainingTrades")
        if remaining is not None and remaining <= 0:
            return False, "서버 시간당 거래 횟수 소진"
        cooldowns = {x["symbol"]: x["tradableAt"] for x in limits.get("cooldowns", [])}
        if symbol in cooldowns:
            return False, f"종목 쿨다운: {cooldowns[symbol]}"
        total = int(portfolio.get("totalAssetValue", 0))
        cash = int(portfolio.get("availableCash", 0))
        holding = next((h for h in portfolio.get("holdings", []) if h["symbol"] == symbol), {})
        if side == "BUY":
            current_value = int(holding.get("quantity", 0)) * price
            if total and current_value + quantity * price > total * self.s.max_position_pct:
                return False, "종목별 최대 비중 초과"
            if total and cash - quantity * price * 1.00015 < total * self.s.cash_reserve_pct:
                return False, "최소 현금 보유 비율 위반"
        elif side == "SELL":
            if quantity > int(holding.get("availableQuantity", 0)):
                return False, "매도 가능 수량 부족"
        else:
            return False, "잘못된 주문 방향"
        return True, "OK"
