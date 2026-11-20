# 개발 담당: 최준혁
"""Chronological, fee-inclusive comparison with untouched validation sessions."""
from datetime import datetime, time
import math
from zoneinfo import ZoneInfo

from .config import Settings
from .decision_policy import decide
from .indicators import technical_signal
from .news import parse_time
from .position_sizing import buy_order_size

KST = ZoneInfo("Asia/Seoul")
BANDS = (0, .05, .10, .20)


def prepare(histories, *, as_of):
    symbols = sorted(histories)
    bars = {}
    for symbol in symbols:
        bars[symbol] = {}
        for row in histories[symbol]:
            day = parse_time(row["timestamp"]).astimezone(KST).date()
            if day >= as_of.astimezone(KST).date():
                continue
            if day in bars[symbol]:
                raise ValueError("duplicate daily bar")
            if any(isinstance(row[k], bool) or not math.isfinite(float(row[k])) or float(row[k]) <= 0 for k in ("openPrice", "closePrice")):
                raise ValueError("invalid daily prices")
            bars[symbol][day] = row
    calendar = sorted(set.intersection(*(set(rows) for rows in bars.values()))) if symbols else []
    if len(calendar) < 85:
        raise ValueError("At least 85 shared completed sessions required")
    technical = {}
    for index in range(55, len(calendar)):
        for symbol in symbols:
            prior = [bars[symbol][day] for day in calendar[max(0, index-100):index]]
            technical[index, symbol] = technical_signal(prior)
    return symbols, bars, calendar, technical


def simulate(prepared, start, end, *, band=0, protect_sell=False, slippage_bps=0,
             news_features=None, buy_and_hold=False):
    symbols, bars, calendar, technical = prepared
    settings = Settings(_env_file=None, rebalance_band_pct=band, protect_sell_signal=protect_sell)
    cash = peak = 50_000_000
    held = dict.fromkeys(symbols, 0)
    costs = buys = sells = topups = 0
    drawdown = 0
    for index in range(start, end):
        day = calendar[index]
        opens = {s: int(bars[s][day]["openPrice"]) for s in symbols}
        actions = {}
        cutoff = datetime.combine(day, time(9), KST)
        for symbol in symbols:
            news = (news_features or {}).get((day.isoformat(), symbol), {})
            if news and (not news.get("as_of") or parse_time(news["as_of"]) > cutoff):
                raise ValueError("news is unavailable at the simulated decision time")
            actions[symbol] = decide(technical[index, symbol], {}, news, settings)["action"]
            if buy_and_hold:
                actions[symbol] = "BUY" if index == start else "HOLD"
        for side in ("SELL", "BUY"):
            for symbol in symbols:
                if actions[symbol] != side:
                    continue
                factor = 1 + (slippage_bps if side == "BUY" else -slippage_bps) / 10000
                price = max(1, int(round(opens[symbol] * factor)))
                total = cash + sum(held[s] * opens[s] for s in symbols)
                if side == "BUY":
                    quantity = buy_order_size(settings, price,
                        {"totalAssetValue": total, "availableCash": cash},
                        {"quantity": held[symbol]}, len(symbols))["quantity"]
                else:
                    quantity = min(held[symbol], settings.max_order_value_krw // price)
                if not quantity:
                    continue
                amount = quantity * price
                fee = amount * 15 // 100000
                if side == "SELL":
                    fee += amount * 2 // 1000
                    cash += amount - fee
                    held[symbol] -= quantity
                    sells += 1
                else:
                    topups += held[symbol] > 0
                    cash -= amount + fee
                    held[symbol] += quantity
                    buys += 1
                costs += fee
        equity = cash + sum(held[s] * int(bars[s][day]["closePrice"]) for s in symbols)
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak-equity)/peak)
    return {"return_pct": round((equity / 50_000_000 - 1) * 100, 6),
            "ending_equity_krw": equity, "max_drawdown_pct": round(drawdown * 100, 6),
            "orders": buys+sells, "buys": buys, "sells": sells, "topups": topups,
            "cost_krw": costs}


def compare_strategies(histories, *, as_of, news_features=None):
    prepared = prepare(histories, as_of=as_of)
    calendar = prepared[2]
    start, end = 55, len(calendar)
    split = start + (end-start) * 2 // 3
    training = {str(band): simulate(prepared, start, split, band=band,
                                  news_features=news_features) for band in BANDS}
    baseline = training["0"]
    eligible = [band for band in BANDS if training[str(band)]["ending_equity_krw"] >= baseline["ending_equity_krw"]
                and training[str(band)]["max_drawdown_pct"] <= baseline["max_drawdown_pct"]]
    selected = max(eligible, key=lambda band: (training[str(band)]["ending_equity_krw"], -band))
    validation = {"baseline": simulate(prepared, split, end, news_features=news_features),
                  "selected": simulate(prepared, split, end, band=selected, news_features=news_features)}
    stressed = {"baseline": simulate(prepared, split, end, slippage_bps=5, news_features=news_features),
                "selected": simulate(prepared, split, end, band=selected, slippage_bps=5,
                                     news_features=news_features)}
    approved = selected > 0 and all(
        result["selected"]["ending_equity_krw"] > result["baseline"]["ending_equity_krw"]
        and result["selected"]["max_drawdown_pct"] <= result["baseline"]["max_drawdown_pct"]
        for result in (validation, stressed))
    applied = selected if approved else 0
    full = {"baseline": simulate(prepared, start, end, news_features=news_features),
            "selected": simulate(prepared, start, end, band=applied, news_features=news_features),
            "buy_and_hold": simulate(prepared, start, end, buy_and_hold=True)}
    return {"symbols": len(prepared[0]), "start": str(calendar[start]), "end": str(calendar[-1]),
            "training_end": str(calendar[split-1]), "validation_start": str(calendar[split]),
            "training_sessions": split-start, "validation_sessions": end-split,
            "training": training, "selected_on_training": selected, "approved": approved,
            "applied_rebalance_band_pct": applied, "validation": validation,
            "validation_slippage_5bps_per_side": stressed, "full_period": full,
            "news_feature_cases": len(news_features or {}),
            "limitations": ["Daily previous-session signals and next-open fills; not a 15-minute execution replay",
                            "No historical Gemini opinions; fees and sale tax use the classroom model",
                            "News return effects are unmeasured when news_feature_cases is zero",
                            "Five-basis-point slippage is a stress assumption, not measured execution data",
                            "One chronological holdout on the current 40-stock universe; selection bias remains"]}


def main():
    import argparse
    import hashlib
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description="Chronological fee-inclusive position sizing comparison")
    parser.add_argument("candles", type=Path)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.candles.read_bytes()
    result = compare_strategies(json.loads(raw), as_of=parse_time(args.as_of))
    result["dataset_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({k:result[k] for k in ("symbols","start","end","approved","applied_rebalance_band_pct")}))


if __name__ == "__main__":
    main()
