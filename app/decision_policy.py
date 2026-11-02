# 개발 담당: 최준혁
"""Explicit classroom policy joining price signals and economic news features."""
import math


def decide(technical, llm, news, settings):
    def score(value):
        if isinstance(value, bool):
            raise ValueError("boolean score")
        number = float(value)
        if not math.isfinite(number) or not -1 <= number <= 1:
            raise ValueError("invalid score")
        return number

    threshold = getattr(settings, "decision_threshold", .35)
    result = {"policy": "classroom-news-policy-v1", "action": "HOLD", "baseline_action": "HOLD",
              "baseline_score": 0.0, "news_adjustment": 0.0, "score": 0.0,
              "news_changed_action": False, "reason": "유효한 가격 이력 부족", "llm_used": False,
              "news_gate": "price_data_not_ready"}
    if technical.get("data_ready") is not True:
        return result
    try:
        tech = score(technical["score"])
        base = tech
        llm_ready = llm.get("availability") == "ok" and llm.get("confidence", 0) >= settings.min_confidence
        if llm_ready:
            opinion = score(llm["score"])
            weight = getattr(settings, "llm_weight", .2)
            base = (1 - weight) * tech + weight * opinion
            result["llm_used"] = True
        adjustment = 0.0
        result["news_gate"] = "no_economic_evidence"
        if news.get("status") == "observed":
            result["news_gate"] = "insufficient_source_groups"
        if not getattr(settings, "news_strategy_enabled", True):
            result["news_gate"] = "news_strategy_disabled"
        elif result["llm_used"] and llm.get("news_context_used"):
            result["news_gate"] = "already_used_by_llm"
        if (getattr(settings, "news_strategy_enabled", True) and news.get("contribution_allowed")
                and not (result["llm_used"] and llm.get("news_context_used"))):
            adjustment = getattr(settings, "news_max_adjustment", .15) * score(news["score"])
            result["news_gate"] = "economic_adjustment_applied"
        combined = max(-1, min(1, base + adjustment))
        def action(value):
            # News never initiates an order contrary to the price trend.
            if value >= threshold and tech >= .2:
                return "BUY"
            if value <= -threshold and tech <= -.2:
                return "SELL"
            return "HOLD"
        baseline_action, final_action = action(base), action(combined)
        result.update(action=final_action, baseline_action=baseline_action, baseline_score=base,
                      news_adjustment=adjustment, score=combined,
                      news_changed_action=baseline_action != final_action,
                      reason=f"기준 {base:.3f} + 뉴스 {adjustment:+.3f} = {combined:.3f}; 독립 출처 {news.get('eligible_sources', 0)}개")
    except (KeyError, ValueError, TypeError):
        result["reason"] = "점수 형식 오류: HOLD"
    return result
