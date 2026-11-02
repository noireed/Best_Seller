from app.indicators import technical_signal

def test_short_history_is_hold():
    candles = [{"timestamp": f"2026-01-{i+1:02d}T09:00:00+09:00",
                "closePrice": 100+i} for i in range(10)]
    result = technical_signal(candles)
    assert result["signal"] == "HOLD"
    assert result["score"] == 0
