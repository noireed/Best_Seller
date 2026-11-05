import logging
from datetime import datetime, timezone, timedelta

from .indicators import technical_signal
from .risk_manager import RiskManager
from .economic_news import economic_indicators
from .decision_policy import decide
from .dart_analysis import validate_news
from .policy_comparison import compare_case, policy_settings
from .event_extraction import assertion_status
from .news import load_aliases, parse_time

log = logging.getLogger(__name__)


class TradingEngine:
    def __init__(self, settings, api, llm, store, news=None, clock=None, dart=None):
        self.s, self.api, self.llm, self.store, self.news = settings, api, llm, store, news
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.dart = dart
        self.risk = RiskManager(settings)

    def run_cycle(self, scan_only=False):
        cutoff = self.clock()
        try:
            portfolio, limits = self.api.portfolio(), self.api.limits()
            rows = self.api.prices(self.s.symbols)["result"]
            prices = {row["symbol"]: row for row in rows}
        except Exception as exc:
            log.warning("사이클 조회 실패: %s", type(exc).__name__)
            return [{"action": "HOLD", "quantity": 0, "execution": "DATA_ERROR",
                     "reason": "초기 조회 실패; 주문 없음"}]
        aliases = {}
        for field in ("news_symbol_aliases", "gnews_symbol_aliases"):
            for symbol, names in load_aliases(getattr(self.s, field, "{}")).items():
                aliases.setdefault(symbol, []).extend(names)
        results = []
        for symbol in self.s.symbols:
            try:
                quote = prices.get(symbol, {})
                price = int(quote.get("price", 0))
                if price <= 0:
                    raise ValueError("missing price")
                candles = self.api.candles(symbol, "1d", self.s.candle_count)["candles"]
                if any(parse_time(c["timestamp"]) > cutoff for c in candles):
                    raise ValueError("future candle")
                tech = technical_signal(candles)
                if candles and max(parse_time(c["timestamp"]) for c in candles) < cutoff - timedelta(days=7):
                    tech = {"score": 0.0, "data_ready": False, "reason": "캔들 이력 지연"}
                report = self.news.collect(symbol) if self.news else None
                if report:
                    self.store.news(report)
                economics = economic_indicators(report, symbol, aliases.get(symbol, []), now=self.clock(),
                    half_life_hours=getattr(self.s, "news_half_life_hours", 12),
                    max_age_hours=getattr(self.s, "news_max_age_hours", 48),
                    allow_delayed=getattr(self.s, "news_allow_delayed_in_strategy", True),
                    min_sources=getattr(self.s, "news_min_sources", 2))
                dart_report = None
                if self.dart:
                    try:
                        dart_report = self.dart.collect(symbol)
                    except Exception:
                        dart_report = {"symbol": symbol, "status": "unavailable", "reason": "DART_SERVICE_ERROR"}
                economics = validate_news(economics, dart_report, now=self.clock(),
                                          min_sources=getattr(self.s, "news_min_sources", 2))
                summary = {k: portfolio.get(k) for k in ("totalAssetValue", "availableCash", "holdings")}
                context = None
                if report and report.get("status") == "ok" and getattr(self.s, "news_use_in_llm", False):
                    blocked_urls = economics["dart_validation"]["blocked_urls"]
                    uncertain_urls = {e["url"] for e in economics["evidence"] if e.get("uncertain")}
                    eligible = [a for a in report["articles"] if a.get("url") not in blocked_urls
                                and a.get("url") not in uncertain_urls
                                and assertion_status(a.get("title", "")) == "reported"
                                and a.get("provider") in ("gnews", "fixture")
                                and a.get("observed_at") and parse_time(a["observed_at"]) <= self.clock()
                                and (not a.get("delay_hours", 0) or getattr(self.s, "news_allow_delayed_in_llm", False))]
                    if eligible:
                        context = {**report, "articles": eligible}
                llm = (self.llm.assess(symbol, tech, price, summary, news=context) if context
                       else self.llm.assess(symbol, tech, price, summary))
                decision = decide(tech, llm, economics, self.s)
                action = decision["action"]
                holding = next((h for h in portfolio.get("holdings", []) if h["symbol"] == symbol), {})
                quantity = 0
                if action == "BUY":
                    cash = max(0, portfolio.get("availableCash", 0) - portfolio.get("totalAssetValue", 0) * self.s.cash_reserve_pct)
                    room = max(0, portfolio.get("totalAssetValue", 0) * self.s.max_position_pct - holding.get("quantity", 0) * price)
                    quantity = int(min(self.s.max_order_value_krw, cash / 1.00015, room) // price)
                elif action == "SELL":
                    quantity = min(int(holding.get("availableQuantity", 0)), self.s.max_order_value_krw // price)
                self.store.decision(symbol, tech, llm, action, decision["reason"])
                item = {"symbol": symbol, "price": price, "action": action, "quantity": quantity,
                        "score": decision["score"], "reason": decision["reason"],
                        "decision": decision, "economic_news": economics,
                        "dart": dart_report or {"status": "disabled"},
                        "news": {"status": report.get("status") if report else "disabled",
                                 "count": len(report.get("articles", [])) if report else 0,
                                 "used_in_llm": bool(context)}, "execution": "SCAN_ONLY"}
                if getattr(self.s, "research_comparison_enabled", True):
                    research_time = self.clock().isoformat()
                    research_input = {"id": symbol + ":" + research_time, "symbol": symbol,
                        "as_of": research_time, "aliases": aliases.get(symbol, []),
                        "technical": tech, "news": report, "dart": dart_report}
                    try:
                        research_input["policy_settings"] = policy_settings(self.s)
                        item["policy_comparison"] = compare_case(research_input, self.s)
                        item["research_input"] = research_input
                    except Exception:
                        # A research/export failure must not interrupt the trading policy.
                        item["policy_comparison"] = {"status": "unavailable"}
                if not scan_only and quantity:
                    try:
                        quote_age = (self.clock() - parse_time(quote["timestamp"])).total_seconds()
                        quote_ok = 0 <= quote_age <= getattr(self.s, "max_price_age_seconds", 900)
                    except (KeyError, ValueError, TypeError):
                        quote_ok = False
                    ok, why = self.risk.check_order(action, symbol, quantity, price, portfolio, limits, now=self.clock())
                    if not quote_ok:
                        item["execution"] = "BLOCKED: 시세 시각 누락·지연·미래"
                    elif not ok:
                        item["execution"] = "BLOCKED: " + why
                    elif self.s.dry_run or not self.s.enable_trading:
                        item["execution"] = "DRY_RUN"
                    elif self.store.unresolved_orders():
                        item["execution"] = "BLOCKED: 결과 미확정 주문 확인 필요"
                    else:
                        identity = self.store.begin_order(symbol, item)
                        try:
                            order = self.api.place_order(symbol, action, quantity)
                            if order.get("status") not in ("OPEN", "PARTIAL", "FILLED", "CANCELLED") or not order.get("orderId"):
                                raise ValueError("unknown submission response")
                            item["execution"] = order["status"]
                            self.store.order(symbol, action, quantity, price, item["execution"], order)
                            self.store.finish_order(identity, "RESPONDED", order)
                            portfolio, limits = self.api.portfolio(), self.api.limits()
                        except Exception as exc:
                            item["execution"] = "UNKNOWN_ORDER_STATE"
                            self.store.finish_order(identity, "UNKNOWN", {"error_type": type(exc).__name__})
                            self.store.policy_decision(symbol, item)
                            results.append(item)
                            break
                elif not scan_only:
                    item["execution"] = "NO_ORDER"
                self.store.policy_decision(symbol, item)
                results.append(item)
            except Exception as exc:
                log.warning("종목 %s 분석 실패: %s", symbol, type(exc).__name__)
                results.append({"symbol": symbol, "action": "HOLD", "quantity": 0,
                                "execution": "DATA_ERROR", "reason": "종목 입력 오류; 주문 없음"})
        return results
