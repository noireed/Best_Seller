"""Compare price rules with next-session fills and AQuA trading costs."""
import argparse
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

from .config import Settings
from .decision_policy import decide
from .indicators import technical_signal
from .news import parse_time


def compare(histories, *, initial_cash=50_000_000, days=60):
    symbols = sorted(histories)
    bars = {symbol: {parse_time(row["timestamp"]).astimezone(ZoneInfo("Asia/Seoul")).date(): row
                     for row in histories[symbol]} for symbol in symbols}
    calendar = sorted(set.intersection(*(set(rows) for rows in bars.values()))) if symbols else []
    if len(calendar) < 56:
        raise ValueError("At least 56 shared sessions required")
    start = max(55, len(calendar) - days)
    settings = Settings(_env_file=None, decision_threshold=.35)
    results = {}
    for rule in ("ema_only", "current"):
        signals = {}
        for index in range(start, len(calendar)):
            for symbol in symbols:
                prior = [bars[symbol][day] for day in calendar[max(0, index-100):index]]
                technical = technical_signal(prior)
                if rule == "current":
                    action = decide(technical, {}, {}, settings)["action"]
                else:
                    close, fast, slow = (technical[key] for key in ("close", "ema20", "ema50"))
                    action = "BUY" if close > fast > slow else "SELL" if close < fast else "HOLD"
                signals[index, symbol] = action
        for charge_costs in (False, True):
            cash, peak, max_drawdown, fees, trades = initial_cash, initial_cash, 0, 0, 0
            held = dict.fromkeys(symbols, 0)
            for index in range(start, len(calendar)):
                day = calendar[index]
                opens = {symbol: int(bars[symbol][day]["openPrice"]) for symbol in symbols}
                for side in ("SELL", "BUY"):
                    for symbol in symbols:
                        if signals[index, symbol] != side:
                            continue
                        price = opens[symbol]
                        total = cash + sum(held[s]*opens[s] for s in symbols)
                        if side == "SELL":
                            quantity = min(held[symbol], settings.max_order_value_krw // price)
                        else:
                            reserve = total * settings.cash_reserve_pct
                            target = min(total * settings.max_position_pct, (total-reserve) / len(symbols))
                            quantity = max(0, int(min(settings.max_order_value_krw,
                                max(0, cash-reserve) / (1.00015 if charge_costs else 1),
                                target - held[symbol]*price) // price))
                        if not quantity:
                            continue
                        amount = quantity * price
                        cost = amount * 15 // 100000 if charge_costs else 0
                        if side == "SELL":
                            cost += amount * 2 // 1000 if charge_costs else 0
                            cash += amount - cost
                            held[symbol] -= quantity
                        else:
                            cash -= amount + cost
                            held[symbol] += quantity
                        fees += cost
                        trades += 1
                equity = cash + sum(held[s]*int(bars[s][day]["closePrice"]) for s in symbols)
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, (peak-equity)/peak)
            results[f"{rule}_{'net' if charge_costs else 'gross'}"] = {
                "return_pct": round((equity/initial_cash-1)*100, 4),
                "max_drawdown_pct": round(max_drawdown*100, 4), "orders": trades, "cost_krw": fees}
    return {"symbols": len(symbols), "sessions": len(calendar)-start, "start": str(calendar[start]),
            "end": str(calendar[-1]), "initial_cash_krw": initial_cash, "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candles", type=Path)
    args = parser.parse_args()
    histories = json.loads(args.candles.read_text(encoding="utf-8"))
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    histories = {s: [r for r in rows if parse_time(r["timestamp"]).astimezone(ZoneInfo("Asia/Seoul")).date() < today]
                 for s, rows in histories.items()}
    print(json.dumps(compare(histories), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
