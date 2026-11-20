# 개발 담당: 최준혁
"""Descriptive real filing-date outcomes, strictly completed before a decision."""
from datetime import datetime
from zoneinfo import ZoneInfo

from .news import parse_time

KST = ZoneInfo("Asia/Seoul")


def filing_outcomes(histories, filings, *, as_of):
    """No intraday release times: always enter the session AFTER the filing date."""
    prices = {}
    for symbol, candles in histories.items():
        prices[symbol] = {parse_time(c["timestamp"]).astimezone(KST).date(): c for c in candles
                          if parse_time(c["timestamp"]) < as_of
                          and parse_time(c["timestamp"]).astimezone(KST).date() < as_of.astimezone(KST).date()}
    calendar = sorted(set.intersection(*(set(p) for p in prices.values()))) if prices else []
    output = {}
    for symbol, rows in filings.items():
        if symbol not in prices:
            continue
        cases, seen = [], set()
        for filing in rows:
            receipt = filing.get("receipt")
            if receipt in seen:
                continue
            seen.add(receipt)
            day = datetime.strptime(filing["date"], "%Y%m%d").date()
            entry_index = next((i for i, date in enumerate(calendar) if date > day), None)
            if entry_index is None or day < calendar[0]:
                continue
            entry = calendar[entry_index]
            horizons = {}
            for sessions in (1, 5, 20):
                end_index = entry_index + sessions - 1
                if end_index >= len(calendar):
                    continue
                end = calendar[end_index]
                def gain(s):
                    return float(prices[s][end]["closePrice"]) / float(prices[s][entry]["openPrice"]) - 1
                gross = gain(symbol)
                # Classroom purchase/sale fee and sale tax, proportional comparison.
                net = (1+gross)*(1-.00015-.002)/(1+.00015)-1
                benchmark = sum(gain(s) for s in prices) / len(prices)
                horizons[str(sessions)] = {"through": str(end), "gross_pct": round(gross*100, 4),
                    "net_pct": round(net*100, 4), "universe_gross_pct": round(benchmark*100, 4),
                    "excess_gross_pct": round((gross-benchmark)*100, 4)}
            if horizons:
                cases.append({"receipt": receipt, "date": str(day), "title": filing["title"],
                              "source": filing["url"], "entry": str(entry), "horizons": horizons})
        output[symbol] = {"cases": sorted(cases, key=lambda c: (c["date"], c["receipt"]), reverse=True)[:12],
                          "as_of": as_of.isoformat(),
                          "limitations": "Current corrected filing list; date-only next-session study. Descriptive associations, not causal effects, historical event sentiment or a trading-return validation. Current watchlist selection bias and unknown price adjustment remain."}
    return output


def available_history(library, symbol, now):
    report = library.get(symbol, {})
    try:
        if not report or parse_time(report["as_of"]) > now:
            return {}
        return {**report, "cases": [{**case, "horizons": {
            horizon: value for horizon, value in case["horizons"].items()
            if datetime.fromisoformat(value["through"]).date() < now.astimezone(KST).date()}}
            for case in report.get("cases", []) if datetime.fromisoformat(case["date"]).date() < now.astimezone(KST).date()]}
    except (KeyError, ValueError, TypeError):
        return {}
