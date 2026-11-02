# 개발 담당: 최준혁
"""Small, explicit economic-event rules for the classroom simulation only.

No forecasts, valuations or learned models are used.
"""
from datetime import datetime, timezone
from difflib import SequenceMatcher
from math import isfinite
import re
from urllib.parse import urlsplit

from .news import parse_time

# These are experimental ordinal rule strengths, NOT returns or probabilities.
RULES = (
    ("revenue_increase", "revenue", 0.5, (r"(?:revenue|sales)\s+(?:rose|grew|jumped|increased)\s+(?:by\s+)?\d+(?:\.\d+)?\s*(?:%|percent)", r"매출[이은는]?\s*\d+(?:\.\d+)?\s*%\s*증가"), "매출 증가 보도이며 순이익·현금흐름 증가로 간주하지 않음"),
    ("revenue_decrease", "revenue", -0.5, (r"(?:revenue|sales)\s+(?:fell|declined|dropped)\s+(?:by\s+)?\d+(?:\.\d+)?\s*(?:%|percent)", r"매출[이은는]?\s*\d+(?:\.\d+)?\s*%\s*감소"), "매출 감소 보도이며 주가 하락률로 환산하지 않음"),
    ("earnings_improvement", "earnings", 0.8, (r"실적\s*개선", r"흑자\s*전환", r"record profits?", r"profit growth"), "이익 개선 보도"),
    ("earnings_deterioration", "earnings", -0.8, (r"실적\s*악화", r"적자\s*전환", r"영업손실", r"net loss", r"profit warning"), "이익 악화 보도"),
    ("contract_win", "revenue", 0.6, (r"수주\s*(확대|성공|체결)", r"공급계약\s*체결", r"wins?\b.{0,30}\bcontract", r"secures?\b.{0,30}\border"), "매출 기회이며 실제 이익·현금 유입은 미확인"),
    ("contract_loss", "revenue", -0.7, (r"계약\s*해지", r"수주\s*취소", r"contract termination", r"order cancellation"), "계약 감소 위험이며 손익 규모는 미확인"),
    ("capital_return", "capital_return", 0.4, (r"배당\s*확대", r"자사주\s*소각", r"dividend increase", r"share buyback"), "주주환원 보도이며 저평가의 증거는 아님"),
    ("operating_risk", "operating_risk", -0.7, (r"리콜", r"조업\s*중단", r"공장\s*화재", r"\brecall\b", r"factory fire", r"production halt"), "생산·제품 비용 위험이며 재무 영향 규모는 미확인"),
)
UNCERTAIN = re.compile(r"전망|예상|가능성|루머|추정|목표주가|매수의견|forecast|expected|rumou?r|may\b|could\b|price target", re.I)
NEGATION = re.compile(r"부인|사실무근|아니|않|없|den(?:y|ies|ied)|\bnot\b|unconfirmed|no longer|ruled out|avoids?\s+(?:a\s+)?(?:recall|net loss|production halt)", re.I)
COMPETITOR = re.compile(r"경쟁사|경쟁업체|competitor|rival", re.I)


def economic_indicators(report, symbol, aliases, *, now=None, half_life_hours=12,
                        max_age_hours=48, allow_delayed=True, min_sources=2):
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    output = {"version": "classroom-economic-events-v1", "symbol": symbol, "as_of": now.isoformat(),
              "status": "no_evidence", "score": 0.0, "eligible_sources": 0,
              "contribution_allowed": False, "evidence": [], "excluded": [],
              "channels": {}, "limitations": ["검색 제목·요약에 대한 규칙 추출", "기사 사실 자체와 미래 손익은 검증하지 않음"]}
    if not report or report.get("symbol") != symbol or report.get("status") != "ok":
        return output
    seen_titles, seen_urls, sources = [], set(), set()
    rows = sorted(report.get("articles", []), key=lambda a: a.get("published_at", ""), reverse=True)
    for article in rows:
        url = article.get("url", "")
        try:
            if article.get("provider") not in ("gnews", "fixture"):
                raise ValueError("provider_not_authorized_for_analysis")
            if article.get("symbol", symbol) != symbol:
                raise ValueError("wrong_symbol")
            published = parse_time(article["published_at"])
            observed = parse_time(article["observed_at"])
            age = (now - published).total_seconds() / 3600
            delay = float(article.get("delay_hours", 0))
            if not isfinite(delay) or delay < 0 or observed < published:
                raise ValueError("invalid_availability")
            if observed > now or age < 0 or age > max_age_hours:
                raise ValueError("outside_information_cutoff")
            if delay and not allow_delayed:
                raise ValueError("delayed_provider")
            title = article["title"]
            matching = [a for a in aliases if a.casefold() in title.casefold()]
            if not matching or any((a + "우") in title for a in matching):
                raise ValueError("company_not_in_title_or_preferred_share")
            if symbol == "005930" and re.search(r"Samsung\s+(Display|SDI|Biologics|Heavy|Life|Fire)|Samsung Electronics preferred", title, re.I):
                raise ValueError("subsidiary_or_preferred_share")
            # Conservative subject rule: another company in the same title is ambiguous.
            if COMPETITOR.search(title):
                raise ValueError("ambiguous_company_attribution")
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
            if urlsplit(url).scheme not in ("http", "https") or not host:
                raise ValueError("invalid_source")
            labels = host.split(".")
            # Conservative grouping for common suffixes; this is not proof of editorial independence.
            suffix_size = 3 if any(host.endswith("." + suffix) for suffix in ("co.kr", "co.uk", "com.au", "co.jp")) else 2
            host = ".".join(labels[-suffix_size:])
            key = re.sub(r"\W", "", title).casefold()
            if url in seen_urls or any(SequenceMatcher(None, key, k).ratio() >= .88 for k in seen_titles):
                raise ValueError("duplicate_or_near_duplicate")
            seen_titles.append(key)
            seen_urls.add(url)
            # Only title clauses containing the target company are actionable.
            # Description polarity can concern other companies and is not scored.
            clauses = [c.strip() for c in re.split(r"[;；。!?]|…", title)
                       if any(a.casefold() in c.casefold() for a in matching)]
            text = " ".join(clauses)
            speculative = bool(UNCERTAIN.search(text) or NEGATION.search(text))
            events = []
            for event_id, channel, impact, patterns, meaning in RULES:
                match = next((m for pattern in patterns if (m := re.search(pattern, text, re.I))), None)
                if match:
                    events.append({"event": event_id, "channel": channel, "rule_strength": impact,
                                   "evidence_text": match.group(), "economic_interpretation": meaning})
                    if event_id in ("revenue_increase", "revenue_decrease"):
                        value = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", match.group(), re.I)
                        numeric_value = float(value.group(1))
                        if not isfinite(numeric_value):
                            raise ValueError("non_finite_reported_change")
                        events[-1]["reported_change"] = {"metric": "revenue", "value": numeric_value,
                            "unit": "percent", "direction": "increase" if impact > 0 else "decrease",
                            "comparison_basis": "year_on_year" if re.search(r"year.over.year|year.on.year|\byoy\b|전년", text, re.I) else "unspecified",
                            "status": "headline_report_not_independently_verified"}
            if not events:
                raise ValueError("no_actionable_economic_event")
            # One article gets one bounded vote even if it contains many keywords.
            values = [e["rule_strength"] for e in events]
            mixed = min(values) < 0 < max(values)
            impact = 0.0 if speculative or mixed else sum(values) / len(values)
            weight = 2 ** (-age / half_life_hours) / (1 + delay / 12)
            record = {"url": url, "title": title, "provider": article["provider"],
                      "published_at": published.isoformat(), "observed_at": observed.isoformat(),
                      "source_host": host, "age_hours": round(age, 3), "delay_hours": delay,
                      "weight": weight, "impact": impact, "uncertain": speculative or mixed,
                      "events": events}
            output["evidence"].append(record)
            if impact:
                sources.add(host)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            output["excluded"].append({"url": url, "reason": str(exc)[:100]})
    # At most one vote per host and event direction: repeated coverage cannot amplify.
    grouped = {}
    for row in output["evidence"]:
        if row["impact"]:
            key = (row["source_host"], 1 if row["impact"] > 0 else -1)
            grouped.setdefault(key, row)
        for event in row["events"]:
            output["channels"].setdefault(event["channel"], []).append(event["event"])
    output["channels"] = {k: sorted(set(v)) for k, v in output["channels"].items()}
    output["eligible_sources"] = len(sources)
    if output["evidence"]:
        output["status"] = "observed"
    if len(sources) >= min_sources and grouped:
        votes = list(grouped.values())
        # Divide by source votes, not sum of age weights: old news loses influence.
        output["score"] = sum(r["impact"] * r["weight"] for r in votes) / len(votes)
        output["contribution_allowed"] = True
        output["status"] = "usable"
    return output
