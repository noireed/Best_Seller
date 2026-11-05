# 개발 담당: 최준혁
"""Conservative financial comparisons; no extra directional trading vote."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re


def _amount(value):
    if not isinstance(value, str) or not re.fullmatch(r"-?\d[\d,]*", value.strip()):
        return None
    try:
        number = Decimal(value.replace(",", ""))
        return int(number) if number.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def financial_metrics(rows, year, month, receipt):
    output = {"status": "incomplete", "year": year, "quarter": month // 3,
              "basis": "consolidated", "receipt": receipt, "currency": "KRW", "metrics": {}}
    annual = month == 12
    # Annual report is a full year. Never label it as standalone Q4.
    output["period"] = "annual" if annual else "quarter"
    for metric, ids, names in (
        ("revenue", {"ifrs-full_Revenue", "ifrs_Revenue"}, {"매출액", "수익(매출액)", "매출", "영업수익"}),
        ("operating_profit", {"dart_OperatingIncomeLoss"}, {"영업이익", "영업이익(손실)", "영업손익"}),
    ):
        matches = [r for r in rows if r.get("sj_div") in ("IS", "CIS") and r.get("currency") == "KRW"
                   and (r.get("account_id") in ids or r.get("account_nm") in names)]
        # Prefer the income statement, then a unique standard account.
        if any(r.get("sj_div") == "IS" for r in matches):
            matches = [r for r in matches if r.get("sj_div") == "IS"]
        standard = [r for r in matches if r.get("account_id") in ids]
        matches = standard or matches
        if len(matches) != 1:
            continue
        row = matches[0]
        current = _amount(row.get("thstrm_amount"))
        previous = _amount(row.get("frmtrm_amount" if annual else "frmtrm_q_amount"))
        cumulative = current if annual else _amount(row.get("thstrm_add_amount"))
        prior_cumulative = previous if annual else _amount(row.get("frmtrm_add_amount"))
        # Negative and zero denominators have no ordinary growth-rate interpretation.
        def growth(value, prior):
            return round((value-prior) / prior * 100, 4) if value is not None and prior is not None and prior > 0 else None
        output["metrics"][metric] = {"current": current, "prior_year_same_period": previous,
            "yoy_pct": growth(current, previous), "cumulative": cumulative,
            "prior_year_cumulative": prior_cumulative, "cumulative_yoy_pct": growth(cumulative, prior_cumulative),
            "profit_transition": ("loss_to_profit" if previous is not None and current is not None and previous < 0 < current
                                  else "profit_to_loss" if previous is not None and current is not None and previous > 0 > current else None)}
    revenue, profit = output["metrics"].get("revenue", {}), output["metrics"].get("operating_profit", {})
    for label, key in (("operating_margin_pct", "current"), ("prior_operating_margin_pct", "prior_year_same_period")):
        r, p = revenue.get(key), profit.get(key)
        output[label] = round(p/r*100, 4) if r is not None and r > 0 and p is not None else None
    current_margin, prior_margin = output["operating_margin_pct"], output["prior_operating_margin_pct"]
    output["operating_margin_change_pp"] = (round(current_margin-prior_margin, 4)
        if current_margin is not None and prior_margin is not None else None)
    if revenue.get("current") is not None and revenue.get("prior_year_same_period") is not None:
        output["status"] = "ok"
    return output


def _claim_period(title):
    years = set(re.findall(r"(?<!\d)(20\d{2})(?:년|\b)", title))
    quarters = set(re.findall(r"\bQ([1-4])\b|([1-4])\s*분기", title, re.I))
    quarters = {a or b for a, b in quarters}
    if len(years) != 1:
        return None
    if re.search(r"연간|full.year|annual|\bFY\b", title, re.I) and not quarters:
        return int(next(iter(years))), 4, "annual"
    if len(quarters) == 1:
        return int(next(iter(years))), int(next(iter(quarters))), "quarter"
    return None


def validate_news(economics, dart_report, *, now=None, min_sources=2):
    """Suppress contradicted revenue claims only when period/basis are explicit.

    An agreement is a numerical comparison, not proof of article truth or returns.
    Unverifiable articles retain the original news policy; DART adds no score.
    """
    output = deepcopy(economics)
    checks, blocked = [], []
    output["dart_validation"] = {"status": "unavailable", "checks": checks, "blocked_urls": blocked}
    if not dart_report or dart_report.get("status") != "ok" or dart_report.get("symbol") != output.get("symbol"):
        return output
    financials = dart_report.get("financials")
    if not financials or financials.get("status") != "ok":
        return output
    now = now or datetime.now(timezone.utc)
    try:
        observed = datetime.fromisoformat(financials["observed_at"])
        filed = datetime.strptime(financials["filing_date"], "%Y%m%d").replace(tzinfo=timezone(timedelta(hours=9)))
        if observed > now or observed < now-timedelta(days=1) or filed > now or filed < now-timedelta(days=180):
            return output
    except (KeyError, ValueError, TypeError):
        return output
    output["dart_validation"]["status"] = "checked"
    for evidence in output.get("evidence", []):
        for event in evidence.get("events", []):
            claim = event.get("reported_change")
            if not claim or claim.get("metric") != "revenue":
                continue
            check = {"url": evidence["url"], "status": "unverifiable", "reason": "period_or_basis_unspecified_or_different",
                     "receipt": financials["receipt"], "filing_url": financials["url"]}
            checks.append(check)
            title = evidence["title"]
            period = _claim_period(title)
            if (claim.get("comparison_basis") != "year_on_year" or
                    period != (financials["year"], financials["quarter"], financials["period"]) or
                    not re.search(r"연결|\bconsolidated\b", title, re.I) or
                    re.search(r"별도|\bstandalone\b|\bforecast\b|전망|예상", title, re.I)):
                continue
            cumulative = bool(re.search(r"누적|\bYTD\b|year.to.date", title, re.I))
            actual = financials["metrics"]["revenue"].get("cumulative_yoy_pct" if cumulative else "yoy_pct")
            if actual is None:
                check["reason"] = "missing_comparable_value_or_nonpositive_base"
                continue
            reported = claim["value"] * (1 if claim["direction"] == "increase" else -1)
            agrees = abs(actual - reported) <= 1.0  # 1 percentage point for headline rounding.
            check.update(status="consistent" if agrees else "contradicted", reason="same_period_consolidated_revenue_yoy",
                         reported_pct=reported, dart_pct=actual, tolerance_pp=1.0,
                         comparison="cumulative" if cumulative else financials["period"])
            if not agrees:
                evidence["impact"] = 0.0
                evidence["dart_excluded"] = True
                if evidence["url"] not in blocked:
                    blocked.append(evidence["url"])
    if blocked:
        grouped = {}
        for row in output["evidence"]:
            if row["impact"]:
                grouped.setdefault((row["source_host"], 1 if row["impact"] > 0 else -1), row)
        votes = list(grouped.values())
        sources = len({row["source_host"] for row in votes})
        output.update(eligible_sources=sources, contribution_allowed=sources >= min_sources,
            score=sum(row["impact"]*row["weight"] for row in votes)/len(votes) if votes and sources >= min_sources else 0.0,
            status="usable" if sources >= min_sources else "observed")
    return output
