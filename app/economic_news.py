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
from .event_extraction import extract_events

def economic_indicators(report, symbol, aliases, *, now=None, half_life_hours=12,
                        max_age_hours=48, allow_delayed=True, min_sources=2):
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    output = {"version": "classroom-economic-events-v2", "symbol": symbol, "as_of": now.isoformat(),
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
            extracted = extract_events(title, matching)
            events = extracted["events"]
            speculative = any(e["assertion"] != "reported" for e in events)
            if not events:
                if extracted["rejected_events"]:
                    raise ValueError("ambiguous_company_attribution:" + extracted["rejected_events"][0]["attribution"]["status"])
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
                      "assertion": extracted["assertion"], "period_mentions": extracted["period_mentions"],
                      "rejected_events": extracted["rejected_events"], "events": events}
            output["evidence"].append(record)
            if impact:
                sources.add(host)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            output["excluded"].append({"url": url, "reason": str(exc)[:100]})
    return rescore(output, min_sources)


def rescore(output, min_sources=2):
    """Recompute the source gate after evidence exclusions or weight reductions."""
    grouped = {}
    sources = set()
    output["channels"] = {}
    for row in output["evidence"]:
        if row["impact"]:
            sources.add(row["source_host"])
            key = (row["source_host"], 1 if row["impact"] > 0 else -1)
            grouped.setdefault(key, row)
        for event in row["events"]:
            output["channels"].setdefault(event["channel"], []).append(event["event"])
    output["channels"] = {k: sorted(set(v)) for k, v in output["channels"].items()}
    output["eligible_sources"] = len(sources)
    output.update(score=0.0, contribution_allowed=False, status="no_evidence")
    if output["evidence"]:
        output["status"] = "observed"
    if len(sources) >= min_sources and grouped:
        votes = list(grouped.values())
        # Divide by source votes, not sum of age weights: old news loses influence.
        output["score"] = sum(r["impact"] * r["weight"] for r in votes) / len(votes)
        output["contribution_allowed"] = True
        output["status"] = "usable"
    return output
