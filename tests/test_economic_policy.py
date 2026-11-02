from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.config import Settings
from app.economic_news import economic_indicators
from app.decision_policy import decide
from app.engine import TradingEngine
from app.news import NaverNewsClient, NewsService
from app.risk_manager import RiskManager
from app.storage import Store
from app.indicators import technical_signal

NOW = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)  # Tuesday noon KST
SETTINGS = Settings(_env_file=None)
ALIASES = ["삼성전자", "Samsung Electronics"]


def article(title, host="one.example", age=1, **extra):
    return {"title": title, "description": "", "url": f"https://{host}/a",
            "provider": "fixture", "published_at": (NOW-timedelta(hours=age)).isoformat(),
            "observed_at": NOW.isoformat(), "delay_hours": 0, **extra}


def report(*articles):
    return {"status": "ok", "symbol": "005930", "as_of": NOW.isoformat(),
            "articles": list(articles), "score": 0}


def indicators(*articles, **kwargs):
    return economic_indicators(report(*articles), "005930", ALIASES, now=NOW, **kwargs)


def positive():
    return indicators(article("삼성전자 실적 개선"), article("삼성전자 공급계약 체결", "two.example"))


def negative():
    return indicators(article("삼성전자 적자 전환"), article("삼성전자 공장 화재", "two.example"))


def test_news_changes_hold_to_buy_and_buy_to_hold():
    d = decide({"score": .30, "data_ready": True}, {}, positive(), SETTINGS)
    assert (d["baseline_action"], d["action"]) == ("HOLD", "BUY")
    assert d["news_changed_action"]
    d = decide({"score": .40, "data_ready": True}, {}, negative(), SETTINGS)
    assert (d["baseline_action"], d["action"]) == ("BUY", "HOLD")


def test_news_supports_sell_but_never_opposes_trend():
    d = decide({"score": -.30, "data_ready": True}, {}, negative(), SETTINGS)
    assert d["action"] == "SELL"
    assert decide({"score": 0, "data_ready": True}, {}, negative(), SETTINGS)["action"] == "HOLD"
    assert decide({"score": .40, "data_ready": True}, {}, negative(), SETTINGS)["action"] != "SELL"


@pytest.mark.parametrize("a,reason", [
    (article("삼성전자 실적 개선", provider="naver"), "provider_not_authorized"),
    (article("삼성전자우 실적 개선"), "preferred_share"),
    (article("삼성전자 경쟁사 실적 개선"), "ambiguous_company"),
    (article("경쟁사 실적 개선"), "company_not_in_title"),
    (article("삼성전자 실적 개선", age=80), "information_cutoff"),
    (article("삼성전자 실적 개선", age=-1), "availability"),
    (article("삼성전자 실적 개선", observed_at=(NOW+timedelta(seconds=1)).isoformat()), "information_cutoff"),
    (article("삼성전자 실적 개선", symbol="000660"), "wrong_symbol"),
])
def test_unusable_evidence_excluded(a, reason):
    r = indicators(a)
    assert r["score"] == 0
    assert reason in r["excluded"][0]["reason"]


@pytest.mark.parametrize("title", ["삼성전자 실적 개선 전망", "삼성전자 리콜 부인", "Samsung Electronics profit growth expected"])
def test_speculation_and_denial_do_not_drive_orders(title):
    r = indicators(article(title))
    assert r["evidence"][0]["impact"] == 0
    assert not r["contribution_allowed"]


def test_duplicate_titles_hosts_and_descriptions_cannot_amplify():
    r = indicators(article("삼성전자 실적 개선"), article("삼성전자 실적 개선", "two.example"))
    assert not r["contribution_allowed"]
    r = indicators(article("삼성전자 실적 개선", "a.publisher.com"),
                   article("삼성전자 공급계약 체결", "b.publisher.com"))
    assert r["eligible_sources"] == 1
    r = indicators(article("삼성전자 신제품 소개", description="경쟁사 적자 전환"))
    assert not r["evidence"]


def test_age_and_delay_reduce_influence_without_lookahead():
    fresh = positive()["score"]
    delayed = indicators(article("삼성전자 실적 개선", age=13, delay_hours=12),
                         article("삼성전자 공급계약 체결", "two.example", age=13, delay_hours=12))
    assert 0 < delayed["score"] < fresh / 2
    disabled = indicators(article("삼성전자 실적 개선", delay_hours=12), allow_delayed=False)
    assert not disabled["evidence"]


@pytest.mark.parametrize("score", [float("nan"), float("inf"), True, 10, "bad"])
def test_invalid_scores_never_trade(score):
    d = decide({"score": score, "data_ready": True}, {}, positive(), SETTINGS)
    assert d["action"] == "HOLD"


def test_llm_optional_and_news_switch():
    tech = {"score": .4, "data_ready": True}
    assert decide(tech, {"availability": "disabled"}, {}, SETTINGS)["action"] == "BUY"
    assert decide({"score": 1, "data_ready": False}, {}, positive(), SETTINGS)["action"] == "HOLD"
    settings = SETTINGS.model_copy(update={"news_strategy_enabled": False})
    assert decide({"score": .3, "data_ready": True}, {}, positive(), settings)["action"] == "HOLD"


def test_same_news_not_added_twice_when_llm_already_uses_it():
    llm = {"availability": "ok", "confidence": .9, "score": .5, "news_context_used": True}
    d = decide({"score": .3, "data_ready": True}, llm, positive(), SETTINGS)
    assert d["llm_used"] and d["news_adjustment"] == 0


def test_unresolved_order_persists_across_restart(tmp_path):
    path = str(tmp_path / "intent.db")
    store = Store(path)
    identity = store.begin_order("005930", {"quantity": 1})
    store.finish_order(identity, "UNKNOWN", {"error_type": "Timeout"})
    store.db.close()
    reopened = Store(path)
    assert reopened.unresolved_orders() == 1
    reopened.db.close()


def test_naver_blocked_before_network_or_cache():
    session = Mock()
    svc = NewsService(NaverNewsClient("id", "secret", session=session), {"005930": ALIASES})
    r = svc.collect("005930")
    assert r["status"] == "unavailable" and "RESTRICTED" in r["reason"]
    session.get.assert_not_called()
    assert not svc.cache


def test_naver_cannot_enter_analysis_storage(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    with pytest.raises(ValueError):
        store.news(report(article("삼성전자 실적 개선", provider="naver")))
    store.db.close()


def test_real_engine_connection_changes_decision_and_logs_without_order(tmp_path):
    api, llm, news = Mock(), Mock(), Mock()
    api.portfolio.return_value = {"availableCash": 100000000, "totalAssetValue": 100000000, "holdings": []}
    api.limits.return_value = {"remainingTrades": 30, "cooldowns": []}
    api.prices.return_value = {"result": [{"symbol": "005930", "price": 70000, "timestamp": NOW.isoformat()}]}
    api.candles.return_value = {"candles": []}
    llm.assess.return_value = {"availability": "disabled"}
    news.collect.return_value = report(article("삼성전자 실적 개선"), article("삼성전자 공급계약 체결", "two.example"))
    settings = SETTINGS.model_copy(update={"watchlist": "005930"})
    store = Store(str(tmp_path / "audit.db"))
    with patch("app.engine.technical_signal", return_value={"data_ready": True, "score": .3}):
        r = TradingEngine(settings, api, llm, store, news, clock=lambda: NOW).run_cycle(scan_only=True)[0]
    assert r["action"] == "BUY" and r["quantity"] > 0
    assert r["decision"]["baseline_action"] == "HOLD"
    api.place_order.assert_not_called()
    assert store.db.execute("SELECT COUNT(*) FROM policy_decisions").fetchone()[0] == 1
    store.db.close()


def test_missing_limits_and_bad_holdings_block_orders():
    risk = RiskManager(SETTINGS)
    portfolio = {"totalAssetValue": 100000000, "availableCash": 100000000, "holdings": []}
    limits = {"remainingTrades": 30, "cooldowns": []}
    assert risk.check_order("BUY", "005930", 1, 70000, portfolio, limits, now=NOW)[0]
    assert not risk.check_order("BUY", "005930", 1, 70000, portfolio, {}, now=NOW)[0]
    assert not risk.check_order("BUY", "005930", 1, 70000, {}, limits, now=NOW)[0]
    assert not risk.check_order("BUY", "005930", 1, 70000, portfolio, limits, now=NOW+timedelta(days=4))[0]


def test_cooldown_expiry_and_fee_cash_check():
    risk = RiskManager(SETTINGS)
    portfolio = {"totalAssetValue": 1000000, "availableCash": 100000, "holdings": []}
    limits = {"remainingTrades": 30, "cooldowns": [{"symbol": "005930", "tradableAt": (NOW+timedelta(minutes=10)).isoformat()}]}
    assert not risk.check_order("BUY", "005930", 1, 10000, portfolio, limits, now=NOW)[0]
    limits["cooldowns"][0]["tradableAt"] = (NOW-timedelta(minutes=1)).isoformat()
    settings = SETTINGS.model_copy(update={"cash_reserve_pct": 0})
    assert RiskManager(settings).check_order("BUY", "005930", 1, 10000, portfolio, limits, now=NOW)[0]
    assert not RiskManager(settings).check_order("BUY", "005930", 1, 100000, portfolio, limits, now=NOW)[0]


def test_rsi_monotonic_and_flat_histories():
    def candles(flat=False):
        return [{"timestamp": (NOW-timedelta(days=80-i)).isoformat(), "closePrice": 100 if flat else 100+i} for i in range(80)]
    assert technical_signal(candles())["rsi14"] == 100
    assert technical_signal(candles(True))["rsi14"] == 50
