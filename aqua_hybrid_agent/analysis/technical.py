import pandas as pd

def indicators(candles: pd.DataFrame) -> dict:
    """Requires chronological candles with close and volume columns."""
    if len(candles) < 35:
        raise ValueError("Need >=35 daily candles")
    c = candles["close"].astype(float)
    v = candles["volume"].astype(float)
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - 100 / (1 + rs)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    return {"rsi14": float(rsi.iloc[-1]), "macd": float(macd.iloc[-1]),
            "macd_signal": float(signal.iloc[-1]),
            "ma20": float(c.rolling(20).mean().iloc[-1]),
            "volume_ratio": float(v.iloc[-1] / v.iloc[-21:-1].mean()),
            "momentum5": float(c.iloc[-1] / c.iloc[-6] - 1)}
