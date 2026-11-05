# 개발 담당: 최준혁
"""Recompute recorded evidence without additional network/LLM calls."""
from .economic_news import economic_indicators
from .decision_policy import decide
from .dart_analysis import validate_news


def explain(technical, llm, report, symbol, aliases, settings, *, now, dart_report=None):
    def compute(data):
        features = economic_indicators(data, symbol, aliases, now=now,
            half_life_hours=settings.news_half_life_hours, max_age_hours=settings.news_max_age_hours,
            allow_delayed=settings.news_allow_delayed_in_strategy, min_sources=settings.news_min_sources)
        features = validate_news(features, dart_report, now=now, min_sources=settings.news_min_sources)
        return features, decide(technical, llm, features, settings)
    features, decision = compute(report)
    result = {"kind": "RECORDED_INPUT_SENSITIVITY_NOT_MARKET_CAUSAL_EFFECT",
              "technical": technical, "decision": decision, "economic_news": features,
              "removals": [], "network_calls": 0, "llm_calls": 0}
    if llm.get("news_context_used") and decision["llm_used"]:
        result["removal_status"] = "unsupported_without_regenerating_llm_opinion"
        return result
    result["removal_status"] = "computed"
    urls = list(dict.fromkeys(row["url"] for row in features["evidence"]))
    for url in urls:
        reduced = {**report, "articles": [a for a in report["articles"] if a.get("url") != url]}
        _, after = compute(reduced)
        result["removals"].append({"removed_url": url, "action_without_article": after["action"],
            "score_without_article": after["score"], "action_changed": after["action"] != decision["action"],
            "news_adjustment_change": after["news_adjustment"]-decision["news_adjustment"]})
    return result
