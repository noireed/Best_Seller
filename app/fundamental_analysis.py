# 개발 담당: 최준혁
"""Explain financial tensions; only attenuate comparable positive news, never add a buy vote."""
from copy import deepcopy
from datetime import timedelta
import re

from .dart_analysis import _claim_period
from .economic_news import rescore
from .news import parse_time


def review_financials(financials):
    metrics = financials.get("metrics", {})
    revenue, profit = metrics.get("revenue", {}), metrics.get("operating_profit", {})
    facts, cautions = [], []
    if revenue.get("yoy_pct") is not None:
        facts.append({"metric": "revenue_yoy_pct", "value": revenue["yoy_pct"]})
    if profit.get("yoy_pct") is not None:
        facts.append({"metric": "operating_profit_yoy_pct", "value": profit["yoy_pct"]})
    if financials.get("operating_margin_change_pp") is not None:
        facts.append({"metric": "operating_margin_change_pp", "value": financials["operating_margin_change_pp"]})
    if (revenue.get("yoy_pct") or 0) > 0 and (profit.get("yoy_pct") is not None and profit["yoy_pct"] < 0):
        cautions.append("revenue_growth_with_profit_decline")
    if profit.get("current") is not None and profit["current"] < 0:
        cautions.append("operating_loss")
    if (financials.get("operating_margin_change_pp") or 0) < 0:
        cautions.append("operating_margin_decline")
    balance = financials.get("balance_and_cash_flow", {})
    for metric in ("current_ratio", "liabilities_to_equity_ratio", "operating_cash_flow_ytd"):
        if balance.get(metric) is not None:
            facts.append({"metric": metric, "value": balance[metric]})
    if balance.get("equity") is not None and balance["equity"] <= 0:
        cautions.append("nonpositive_equity")
    if balance.get("operating_cash_flow_ytd") is not None and balance["operating_cash_flow_ytd"] < 0:
        cautions.append("negative_operating_cash_flow_ytd")
    if balance.get("current_ratio") is not None and balance["current_ratio"] < 1:
        cautions.append("current_assets_below_current_liabilities")
    return {"status": "observed", "receipt": financials.get("receipt"), "facts": facts, "cautions": cautions,
            "interpretation": "Revenue growth alone does not establish profit or cash-flow improvement.",
            "limitations": "No valuation, consensus surprise or return prediction. Ratios need industry context; year-to-date cash flow is not standalone quarterly profit."}


def apply_fundamentals(features, dart_report, *, now, enabled=True, min_sources=2):
    result = deepcopy(features)
    metadata = {"status": "unavailable", "adjusted_urls": [], "factor": .5,
                "policy": "experimental_comparable_positive_news_attenuation_not_return_estimate"}
    result["fundamental_guard"] = metadata
    if not enabled:
        metadata["status"] = "disabled"
        return result
    if not dart_report or dart_report.get("status") != "ok" or dart_report.get("symbol") != result["symbol"]:
        return result
    financials = dart_report.get("financials")
    if not financials or financials.get("status") != "ok":
        return result
    try:
        observed = parse_time(financials["observed_at"])
        from datetime import datetime, timezone
        filed = datetime.strptime(financials["filing_date"], "%Y%m%d").replace(tzinfo=timezone(timedelta(hours=9)))
        if not now-timedelta(days=1) <= observed <= now or not now-timedelta(days=180) <= filed <= now:
            return result
    except (ValueError, TypeError, KeyError):
        return result
    review = review_financials(financials)
    metadata.update(status="reviewed", receipt=financials["receipt"], review=review)
    if not set(review["cautions"]) & {"revenue_growth_with_profit_decline", "operating_loss"}:
        return result
    for row in result["evidence"]:
        title = row["title"]
        if (row["impact"] <= 0 or not re.search(r"연결|\bconsolidated\b", title, re.I)
                or re.search(r"별도|standalone|누적|\bYTD\b", title, re.I)
                or _claim_period(title) != (financials["year"], financials["quarter"], financials["period"])):
            continue
        # Only comparable revenue/earnings claims; a contract or a dividend is not this period's income.
        if any(e.get("reported_change", {}).get("metric") not in {"revenue", "operating_profit"} or
               e["reported_change"].get("comparison_basis") != "year_on_year" for e in row["events"]):
            continue
        row["impact"] *= metadata["factor"]
        row["fundamental_attenuation"] = metadata["factor"]
        metadata["adjusted_urls"].append(row["url"])
    return rescore(result, min_sources)
