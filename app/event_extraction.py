# 개발 담당: 최준혁
"""Title-only economic extraction with assertion status and source spans."""
from math import isfinite
import re
from .company_attribution import attribute_event, mentions

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

WITHDRAWN = re.compile(r"철회|취소된?\s*전망|withdraw(?:s|n|ing)?|retract(?:s|ed)?", re.I)
FORECAST = re.compile(r"전망|예상|추정|forecast|expected|expects?|projects?|\bwill\b|outlook", re.I)
HEDGE = re.compile(r"가능성|루머|목표주가|매수의견|rumou?r|\bmay\b|\bcould\b|price target", re.I)
NEGATED = re.compile(r"부인|사실무근|아니|않|없|den(?:y|ies|ied)|\bnot\b|unconfirmed|no longer|ruled out|avoids?\s+(?:a\s+)?(?:recall|net loss|production halt)", re.I)

# Percentage movement patterns retain the metric and direction within the same match.
MOVEMENTS = (
    ("revenue", "revenue", r"(?:revenue|sales)", r"매출(?:액)?"),
    ("operating_profit", "earnings", r"operating profit", r"영업이익"),
)


def assertion_status(text):
    if WITHDRAWN.search(text):
        return "withdrawn"
    if NEGATED.search(text):
        return "denied"
    if FORECAST.search(text):
        return "forecast"
    if HEDGE.search(text):
        return "uncertain"
    return "reported"


def _change(match, metric, direction, clause):
    value = float(match.group("value"))
    if not isfinite(value):
        raise ValueError("non_finite_reported_change")
    yoy = bool(re.search(r"year.over.year|year.on.year|\byoy\b|from last year|전년", clause, re.I))
    qoq = bool(re.search(r"quarter.over.quarter|quarter.on.quarter|\bqoq\b|전분기", clause, re.I))
    return {"metric": metric, "value": value, "unit": "percent", "direction": direction,
            "comparison_basis": "ambiguous" if yoy and qoq else "year_on_year" if yoy else "quarter_on_quarter" if qoq else "unspecified",
            "status": "headline_report_not_independently_verified"}


def extract_events(title, aliases):
    if not isinstance(title, str) or len(title) > 10000:
        raise ValueError("invalid_title")
    aliases = [a for a in aliases if isinstance(a, str) and a.strip()]
    events, rejected = [], []
    for clause_match in re.finditer(r"[^;；。!?…]+", title):
        clause = clause_match.group()
        actors = [name for _, _, name in mentions(clause, aliases)]
        if not actors:
            continue
        actor = max(actors, key=len)
        status = assertion_status(clause)
        def record(event_id, channel, impact, match, meaning, change=None):
            start, end = clause_match.start()+match.start(), clause_match.start()+match.end()
            attribution = attribute_event(clause, aliases, match.start(), match.end())
            attribution["clause_span"] = [clause_match.start(), clause_match.end()]
            for mention in attribution["target_mentions"]:
                mention["span"] = [value+clause_match.start() for value in mention["span"]]
            event = {"event": event_id, "channel": channel, "rule_strength": impact,
                "actor": attribution["actor"] or actor, "assertion": status, "evidence_text": title[start:end],
                "attribution": attribution,
                "span": [start, end], "economic_interpretation": meaning}
            if change:
                event["reported_change"] = change
            (events if attribution["status"] == "direct" else rejected).append(event)
        for metric, channel, english, korean in MOVEMENTS:
            for direction, en, ko, impact in (
                ("increase", r"rose|grew|jumped|increased|grow|rise|increase|(?:was\s+)?up", "증가", .5 if metric == "revenue" else .8),
                ("decrease", r"fell|declined|dropped|fall|decline|drop|(?:was\s+)?down", "감소", -.5 if metric == "revenue" else -.8),
            ):
                patterns = (rf"{english}\s+(?:(?:is\s+)?expected\s+to\s+|will\s+)?(?:{en})\s+(?:by\s+)?(?P<value>\d+(?:\.\d+)?)\s*(?:%|percent)",
                            rf"{korean}[이은는]?\s*(?:(?:전년(?:\s*동기)?|전분기)\s*(?:대비)?\s*)?(?P<value>\d+(?:\.\d+)?)\s*%\s*{ko}")
                match = next((m for p in patterns if (m := re.search(p, clause, re.I))), None)
                if match:
                    event_id = ("revenue_" + direction if metric == "revenue" else
                                "earnings_improvement" if direction == "increase" else "earnings_deterioration")
                    record(event_id, channel, impact, match, "보도된 변화율이며 미래 수익률로 환산하지 않음",
                           _change(match, metric, direction, clause))
        for event_id, channel, impact, patterns, meaning in RULES:
            if event_id.startswith("revenue_") or any(e["event"] == event_id and e["span"][0] >= clause_match.start() for e in events):
                continue
            match = next((m for p in patterns if (m := re.search(p, clause, re.I))), None)
            if match:
                record(event_id, channel, impact, match, meaning)
    states = {e["assertion"] for e in events}
    return {"version": "economic-extraction-v3", "events": events, "rejected_events": rejected,
            "assertion": next(iter(states)) if len(states) == 1 else "mixed" if states else "none",
            "period_mentions": list(dict.fromkeys(m.group() for m in re.finditer(r"20\d{2}년?|\bQ[1-4]\b|[1-4]\s*분기|연간|\bYTD\b", title, re.I))),
            "limitations": "Rule-based title extraction; reported does not mean independently verified."}
