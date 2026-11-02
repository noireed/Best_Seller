import pandas as pd

def technical_signal(candles):
    if len(candles) < 55:
        return {"score": 0.0, "signal": "HOLD", "reason": "캔들 데이터 부족"}
    df = pd.DataFrame(candles).sort_values("timestamp").copy()
    close = pd.to_numeric(df["closePrice"], errors="coerce")
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - 100 / (1 + rs)
    momentum = close.pct_change(20)
    score, reasons = 0.0, []
    if close.iloc[-1] > ema20.iloc[-1] > ema50.iloc[-1]:
        score += .35; reasons.append("상승 EMA 정렬")
    elif close.iloc[-1] < ema20.iloc[-1] < ema50.iloc[-1]:
        score -= .35; reasons.append("하락 EMA 정렬")
    rv = rsi.iloc[-1]
    if pd.notna(rv):
        if 45 <= rv <= 65: score += .20
        elif rv >= 75: score -= .25
        elif rv <= 25: score += .10
        reasons.append(f"RSI14={rv:.1f}")
    mv = momentum.iloc[-1]
    if pd.notna(mv):
        score += max(-.25, min(.25, float(mv) * 2))
        reasons.append(f"20봉 모멘텀={mv:.2%}")
    score = max(-1, min(1, score))
    signal = "BUY" if score >= .35 else "SELL" if score <= -.35 else "HOLD"
    return {"score": score, "signal": signal, "reason": ", ".join(reasons),
            "rsi14": None if pd.isna(rv) else float(rv),
            "momentum20": None if pd.isna(mv) else float(mv),
            "close": float(close.iloc[-1])}
