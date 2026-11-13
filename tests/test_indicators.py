from app.indicators import technical_signal
from datetime import datetime, timedelta, timezone

def test_short_history_is_hold():
    candles = [{"timestamp": f"2026-01-{i+1:02d}T09:00:00+09:00",
                "closePrice": 100+i} for i in range(10)]
    result = technical_signal(candles)
    assert result["signal"] == "HOLD"
    assert result["score"] == 0


def test_trend_and_momentum_confirm_trade_direction():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for direction, expected in [(1, "BUY"), (-1, "SELL")]:
        prices = [1000]
        for i in range(79):
            prices.append(prices[-1] + direction * (2 if i % 2 == 0 else -1))
        candles = [{"timestamp": (start + timedelta(days=i)).isoformat(), "closePrice": price}
                   for i, price in enumerate(prices)]
        assert technical_signal(candles)["signal"] == expected
