import pandas as pd

def technical_signal(candles):
    if len(candles) < 55:
        return {"score": 0.0, "signal": "HOLD", "reason": "캔들 데이터 부족", "data_ready": False}
    df = pd.DataFrame(candles).copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    if df["timestamp"].isna().any():
        return {"score": 0.0, "signal": "HOLD", "reason": "캔들 시각 오류", "data_ready": False}
    df = df.sort_values("timestamp")
    if df["timestamp"].duplicated().any():
        return {"score": 0.0, "signal": "HOLD", "reason": "중복 캔들", "data_ready": False}
    close = pd.to_numeric(df["closePrice"], errors="coerce")
    if close.isna().any() or not ((close > 0) & (close < float("inf"))).all():
        return {"score": 0.0, "signal": "HOLD", "reason": "유효하지 않은 종가", "data_ready": False}
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    delta = close.diff()
    gains = delta.dropna().clip(lower=0)
    losses = -delta.dropna().clip(upper=0)
    def wilder_average(values):
        seeded = values.iloc[13:].copy()
        seeded.iloc[0] = values.iloc[:14].mean()
        return seeded.ewm(alpha=1/14, adjust=False).mean()

    gain = wilder_average(gains)
    loss = wilder_average(losses)
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.mask((loss == 0) & (gain > 0), 100.0)
    rsi = rsi.mask((loss == 0) & (gain == 0), 50.0)
    momentum = close.pct_change(20)
    rv, mv = float(rsi.iloc[-1]), float(momentum.iloc[-1])
    latest, fast, slow = float(close.iloc[-1]), float(ema20.iloc[-1]), float(ema50.iloc[-1])
    score = 0.0
    if latest > fast > slow and mv > 0 and 50 <= rv <= 70:
        score = .35 + min(.25, mv * 2)
        trend = "상승 추세와 모멘텀 확인"
    elif latest < fast and mv < 0 and rv < 50:
        score = -.35 - min(.25, -mv * 2)
        trend = "단기 추세 약화와 음의 모멘텀 확인"
    else:
        trend = "추세·모멘텀 조건 미충족"
    signal = "BUY" if score >= .35 else "SELL" if score <= -.35 else "HOLD"
    return {"score": score, "signal": signal, "reason": f"{trend}, RSI14={rv:.1f}, 20봉 모멘텀={mv:.2%}", "data_ready": True,
            "ema20": fast, "ema50": slow,
            "rsi14": None if pd.isna(rv) else float(rv),
            "momentum20": None if pd.isna(mv) else float(mv),
            "close": float(close.iloc[-1])}
