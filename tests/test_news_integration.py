from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.engine import TradingEngine


@pytest.mark.parametrize("enabled,status,delay,expected", [(False, "ok", 0, False), (True, "ok", 0, True),
    (True, "empty", 0, False), (True, "unavailable", 0, False), (True, "ok", 12, False)])
def test_news_context_is_opt_in_and_scan_never_orders(enabled, status, delay, expected):
    settings = SimpleNamespace(symbols=["005930"], candle_count=100, news_use_in_llm=enabled,
                               min_confidence=0.6, cash_reserve_pct=0.1,
                               max_order_value_krw=2000000, max_position_pct=0.15,
                               news_allow_delayed_in_llm=False)
    api, llm, store, news = Mock(), Mock(), Mock(), Mock()
    api.portfolio.return_value = {"totalAssetValue": 100000000, "availableCash": 100000000}
    api.limits.return_value = {}
    api.prices.return_value = {"result": [{"symbol": "005930", "price": 70000}]}
    api.candles.return_value = {"candles": []}
    llm.assess.return_value = {"score": 1, "confidence": 0.9, "reason": "test"}
    news.collect.return_value = {"symbol": "005930", "as_of": "2026-11-02T12:00:00+09:00",
                                 "status": status, "articles": [{"delay_hours": delay, "provider": "fixture",
                                 "observed_at": "2026-11-02T12:00:00+09:00"}], "score": 0}
    with patch("app.engine.technical_signal", return_value={"score": 1, "reason": "test"}):
        result = TradingEngine(settings, api, llm, store, news).run_cycle(scan_only=True)
    assert result[0]["news"]["used_in_llm"] is expected
    assert ("news" in llm.assess.call_args.kwargs) is expected
    api.place_order.assert_not_called()
    store.news.assert_called_once()


def test_no_news_service_preserves_old_llm_call():
    settings = SimpleNamespace(symbols=["005930"], candle_count=100, min_confidence=0.6)
    api, llm, store = Mock(), Mock(), Mock()
    api.portfolio.return_value = {}
    api.prices.return_value = {"result": [{"symbol": "005930", "price": 70000}]}
    api.candles.return_value = {"candles": []}
    llm.assess.return_value = {"score": 0, "confidence": 0, "reason": "test"}
    result = TradingEngine(settings, api, llm, store).run_cycle(scan_only=True)
    assert len(result) == 1
    assert "news" not in llm.assess.call_args.kwargs
    store.news.assert_not_called()
