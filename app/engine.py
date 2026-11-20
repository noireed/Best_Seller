import logging
from datetime import datetime, timezone, timedelta

from .indicators import technical_signal
from .api_client import APIError
from .risk_manager import RiskManager
from .economic_news import economic_indicators
from .decision_policy import decide
from .position_sizing import buy_order_size
from .dart_analysis import validate_news
from .policy_comparison import compare_case, policy_settings
from .event_extraction import assertion_status
from .event_history import apply_history
from .fundamental_analysis import apply_fundamentals
from .news import load_aliases, parse_time
from .gemini_review import GeminiReview
from .event_outcomes import available_history, filing_outcomes
from .research_universe import COMPANIES
import json
from pathlib import Path

log = logging.getLogger(__name__)


class TradingEngine:
    def __init__(self, settings, api, llm, store, news=None, clock=None, dart=None):
        self.s, self.api, self.llm, self.store, self.news = settings, api, llm, store, news
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.dart = dart
        self.risk = RiskManager(settings)
        self.gemini = GeminiReview(settings, store)
        history_path = Path(__file__).parent.parent / "examples" / "filing_outcomes.json"
        self.historical_events = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}
        recorded = store.outcome_library()
        if isinstance(recorded, dict):
            self.historical_events.update(recorded)

    def run_cycle(self, scan_only=False):
        cutoff = self.clock()
        symbols = self.s.symbols
        try:
            portfolio, limits = self.api.portfolio(), self.api.limits()
            rows = self.api.prices(symbols)["result"]
            prices = {row["symbol"]: row for row in rows}
        except Exception as exc:
            log.warning("사이클 조회 실패: %s (HTTP %s)", type(exc).__name__, getattr(exc, "status", None))
            return [{"action": "HOLD", "quantity": 0, "execution": "DATA_ERROR",
                     "reason": "초기 조회 실패; 주문 없음"}]
        aliases = {}
        held_symbols = {row["symbol"] for row in portfolio.get("holdings", []) if row.get("quantity", 0) > 0}
        symbols = sorted(symbols, key=lambda symbol: symbol not in held_symbols)
        for field in ("news_symbol_aliases", "gnews_symbol_aliases"):
            for symbol, names in load_aliases(getattr(self.s, field, "{}")).items():
                aliases.setdefault(symbol, []).extend(names)
        results = []
        cycle_candles, cycle_filings = {}, {}
        reviews_remaining = getattr(self.s, "gemini_reviews_per_cycle", 40)
        for symbol in symbols:
            try:
                quote = prices.get(symbol, {})
                price = int(quote.get("price", 0))
                if price <= 0:
                    raise ValueError("missing price")
                candles = self.api.candles(symbol, "1d", self.s.candle_count)["candles"]
                if any(parse_time(c["timestamp"]) > cutoff for c in candles):
                    raise ValueError("future candle")
                tech = technical_signal(candles)
                cycle_candles[symbol] = candles
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
                cycle_filings[symbol] = (dart_report or {}).get("filings", [])
                history_snapshot = None
                if report and getattr(self.s, "event_history_enabled", True):
                    try:
                        snapshot = self.store.record_event_history(report, aliases.get(symbol, []), now=self.clock(),
                            max_age_hours=getattr(self.s, "news_max_age_hours", 48))
                        if isinstance(snapshot, dict):
                            history_snapshot = snapshot
                    except Exception:
                        history_snapshot = {"status": "unavailable", "reason": "history_storage_error"}
                economics = apply_history(economics, history_snapshot, now=self.clock(),
                    half_life_hours=getattr(self.s, "news_half_life_hours", 12),
                    max_age_hours=getattr(self.s, "news_max_age_hours", 48), min_sources=getattr(self.s, "news_min_sources", 2))
                economics = apply_fundamentals(economics, dart_report, now=self.clock(),
                    enabled=getattr(self.s, "fundamental_guard_enabled", True), min_sources=getattr(self.s, "news_min_sources", 2))
                summary = {k: portfolio.get(k) for k in ("totalAssetValue", "availableCash", "holdings")}
                context = None
                if report and report.get("status") == "ok" and getattr(self.s, "news_use_in_llm", False):
                    blocked_urls = economics["dart_validation"]["blocked_urls"]
                    blocked_urls = set(blocked_urls) | set(economics["event_history"]["blocked_urls"])
                    blocked_urls |= set(economics["fundamental_guard"]["adjusted_urls"])
                    blocked_urls |= {e["url"] for e in economics["excluded"] if "company" in e["reason"] or "subsidiary" in e["reason"] or "preferred_share" in e["reason"]}
                    uncertain_urls = {e["url"] for e in economics["evidence"] if e.get("uncertain")}
                    uncertain_urls |= {e["url"] for e in economics["evidence"] if e.get("rejected_events")}
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
                research = {"status": "disabled"}
                if getattr(self.s, "intraday_research_enabled", True) and self.gemini.key:
                    try:
                        research = self.gemini.search(symbol, aliases.get(symbol, [COMPANIES.get(symbol, symbol)]), self.clock())
                    except Exception as exc:
                        research = {"status": "research_error", "error_type": type(exc).__name__}
                history = available_history(self.historical_events, symbol, self.clock())
                quantity = 0
                sizing = None
                if action == "BUY":
                    sizing = buy_order_size(self.s, price, portfolio, holding, len(symbols))
                    quantity = sizing["quantity"]
                elif action == "SELL":
                    quantity = min(int(holding.get("availableQuantity", 0)), self.s.max_order_value_krw // price)
                gemini = {"status": "not_reviewed"}
                if (action == "BUY" and (quantity or scan_only) or holding.get("quantity", 0) > 0) and reviews_remaining and self.gemini.key:
                    reviews_remaining -= 1
                    try:
                        gemini = self.gemini.review(symbol, economics, dart_report, self.clock(),
                            technical=tech, proposed_action=action, holding=holding, research=research, history=history)
                    except Exception as exc:
                        gemini = {"status": "review_error", "error_type": type(exc).__name__}
                if action == "BUY":
                    confirmed = gemini.get("status") == "ok" and gemini.get("verdict") == "PASS"
                    if ((getattr(self.s, "gemini_required_for_buy", True) and not confirmed)
                            or gemini.get("verdict") in ("HOLD", "EXIT")):
                        action, quantity = "HOLD", 0
                        decision = {**decision, "action": action, "reason": "Gemini 매수 확인 실패·보류: " + gemini.get("reason", gemini["status"])}
                elif action == "HOLD" and gemini.get("status") == "ok" and gemini.get("verdict") == "EXIT":
                    action = "SELL"
                    quantity = min(int(holding.get("availableQuantity", 0)), self.s.max_order_value_krw // price)
                    decision = {**decision, "action": action, "reason": "당일 경제사건 매도 검토: " + gemini["reason"],
                                "event_exit": True}
                candidate_action = action
                if action == "BUY" and quantity == 0:
                    action = "HOLD"
                    decision = {**decision, "action": action,
                        "reason": "추가 매수 수량 없음: " + sizing["reason"],
                        "sizing_gate": sizing["reason"]}
                self.store.decision(symbol, tech, llm, action, decision["reason"])
                item = {"symbol": symbol, "price": price, "action": action, "quantity": quantity,
                        "candidate_action": candidate_action,
                        "score": decision["score"], "reason": decision["reason"],
                        "technical_reason": tech.get("reason"),
                        "decision": decision, "economic_news": economics,
                        "gemini_review": gemini, "position_sizing": sizing,
                        "market_research": research, "historical_event_outcomes": history,
                        "dart": dart_report or {"status": "disabled"},
                        "news": {"status": report.get("status") if report else "disabled",
                                 "count": len(report.get("articles", [])) if report else 0,
                                 "used_in_llm": bool(context)}, "execution": "SCAN_ONLY"}
                if getattr(self.s, "research_comparison_enabled", True):
                    research_time = self.clock().isoformat()
                    research_input = {"id": symbol + ":" + research_time, "symbol": symbol,
                        "as_of": research_time, "aliases": aliases.get(symbol, []),
                        "technical": tech, "news": report, "dart": dart_report}
                    research_input["event_history"] = history_snapshot
                    try:
                        research_input["policy_settings"] = policy_settings(self.s)
                        item["policy_comparison"] = compare_case(research_input, self.s)
                        item["research_input"] = research_input
                    except Exception:
                        # A research/export failure must not interrupt the trading policy.
                        item["policy_comparison"] = {"status": "unavailable"}
                if not scan_only and quantity:
                    # Grounded research can take time. Refresh the executable quote and size
                    # instead of trading on the cycle's original 40-stock price snapshot.
                    try:
                        latest = self.api.prices([symbol])["result"]
                        quote = next(q for q in latest if q["symbol"] == symbol)
                        price = int(quote["price"])
                        if price <= 0:
                            raise ValueError("invalid refreshed price")
                        if action == "BUY":
                            sizing = buy_order_size(self.s, price, portfolio, holding, len(symbols))
                            quantity = sizing["quantity"]
                            item["position_sizing"] = sizing
                        else:
                            quantity = min(int(holding.get("availableQuantity", 0)), self.s.max_order_value_krw // price)
                        item.update(price=price, quantity=quantity)
                        if action == "BUY" and quantity == 0:
                            item.update(action="HOLD", execution="NO_ORDER",
                                reason="주문 직전 추가 매수 수량 없음: " + sizing["reason"])
                            item["decision"] = {**item["decision"], "action": "HOLD",
                                "sizing_gate": sizing["reason"], "reason": item["reason"]}
                            self.store.policy_decision(symbol, item)
                            results.append(item)
                            continue
                    except Exception as exc:
                        item["execution"] = "BLOCKED: 주문 직전 시세 재확인 실패"
                        item["quote_error"] = type(exc).__name__
                        self.store.policy_decision(symbol, item)
                        results.append(item)
                        continue
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
                        except Exception as exc:
                            rejected = isinstance(exc, APIError) and exc.status in (400, 401, 403, 404, 409, 422, 429)
                            item["execution"] = "REJECTED" if rejected else "UNKNOWN_ORDER_STATE"
                            state = "RESPONDED" if rejected else "UNKNOWN"
                            response = {"error_type": type(exc).__name__, "http_status": getattr(exc, "status", None)}
                            self.store.finish_order(identity, state, response)
                            self.store.policy_decision(symbol, item)
                            results.append(item)
                            break
                        try:
                            portfolio, limits = self.api.portfolio(), self.api.limits()
                        except Exception as exc:
                            log.warning("주문 응답 후 계좌 조회 실패: %s", type(exc).__name__)
                            self.store.policy_decision(symbol, item)
                            results.append(item)
                            break
                elif not scan_only:
                    item["execution"] = "NO_ORDER"
                self.store.policy_decision(symbol, item)
                results.append(item)
                if self.gemini.key and len(results) % 10 == 0:
                    try:
                        self.store.order_checkpoint()
                    except Exception as exc:
                        log.warning("조사 중간 저장 실패: %s", type(exc).__name__)
            except Exception as exc:
                log.warning("종목 %s 분석 실패: %s", symbol, type(exc).__name__)
                results.append({"symbol": symbol, "action": "HOLD", "quantity": 0,
                                "execution": "DATA_ERROR", "reason": "종목 입력 오류; 주문 없음"})
        if len(cycle_candles) == len(symbols) and all(cycle_candles.values()):
            try:
                updated = filing_outcomes(cycle_candles, cycle_filings, as_of=self.clock())
                for symbol, report in updated.items():
                    previous = available_history(self.historical_events, symbol, self.clock())
                    cases = {case["receipt"]: case for case in previous.get("cases", [])}
                    for case in report["cases"]:
                        old = cases.get(case["receipt"], {})
                        cases[case["receipt"]] = {**case, "horizons": {**old.get("horizons", {}), **case["horizons"]}}
                    report["cases"] = sorted(cases.values(), key=lambda c: (c["date"], c["receipt"]), reverse=True)[:12]
                self.store.save_outcomes(updated)
                self.historical_events.update(updated)
            except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
                log.warning("과거 공시 결과 갱신 실패: %s", type(exc).__name__)
        return results
