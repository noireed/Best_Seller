import logging
from .indicators import technical_signal
from .risk_manager import RiskManager
log = logging.getLogger(__name__)

class TradingEngine:
    def __init__(self, settings, api, llm, store):
        self.s, self.api, self.llm, self.store = settings, api, llm, store
        self.risk = RiskManager(settings)

    def run_cycle(self, scan_only=False):
        portfolio = self.api.portfolio()
        limits = self.api.limits()
        rows = self.api.prices(self.s.symbols).get("result", [])
        prices = {x["symbol"]: int(x["price"]) for x in rows}
        summary = {"totalAssetValue": portfolio.get("totalAssetValue"),
                   "availableCash": portfolio.get("availableCash"),
                   "holdings": portfolio.get("holdings", [])}
        results = []
        for symbol in self.s.symbols:
            try:
                price = prices.get(symbol)
                if not price: continue
                candles = self.api.candles(symbol, "1d", self.s.candle_count).get("candles", [])
                tech = technical_signal(candles)
                llm = self.llm.assess(symbol, tech, price, summary)
                combined = .70 * tech["score"] + .30 * llm["score"]
                action = "BUY" if combined >= .35 else "SELL" if combined <= -.35 else "HOLD"
                holding = next((h for h in portfolio.get("holdings", []) if h["symbol"] == symbol), {})
                quantity = 0
                if action == "BUY" and llm["confidence"] >= self.s.min_confidence:
                    cash_budget = max(0, int(portfolio.get("availableCash", 0) -
                                     portfolio.get("totalAssetValue", 0) * self.s.cash_reserve_pct))
                    budget = min(self.s.max_order_value_krw, cash_budget)
                    position_room = max(0, int(portfolio.get("totalAssetValue", 0) *
                                      self.s.max_position_pct - int(holding.get("quantity", 0)) * price))
                    quantity = min(budget, position_room) // price
                elif action == "SELL":
                    quantity = int(holding.get("availableQuantity", 0))
                reason = f"정량 70%({tech['score']:.2f}) + LLM 30%({llm['score']:.2f}) = {combined:.2f}; {tech['reason']} / {llm['reason']}"
                self.store.decision(symbol, tech, llm, action, reason)
                item = {"symbol": symbol, "price": price, "action": action,
                        "score": round(combined, 3), "quantity": int(quantity), "reason": reason}
                if quantity and not scan_only:
                    ok, why = self.risk.check_order(action, symbol, quantity, price, portfolio, limits)
                    if not ok:
                        item["execution"] = "BLOCKED: " + why
                    elif self.s.dry_run or not self.s.enable_trading:
                        item["execution"] = "DRY_RUN: 주문 API 미호출"
                        self.store.order(symbol, action, quantity, price, "DRY_RUN", item)
                    else:
                        # 시장가 주문: API는 현재 호가 잔량만 체결하고 미체결 잔량은 취소할 수 있음
                        order = self.api.place_order(symbol, action, quantity)
                        item["execution"] = order.get("status", "UNKNOWN")
                        self.store.order(symbol, action, quantity, price, order.get("status", "UNKNOWN"), order)
                        portfolio, limits = self.api.portfolio(), self.api.limits()
                results.append(item)
                log.info("%s", item)
            except Exception:
                log.exception("종목 %s 분석 실패", symbol)
        return results
