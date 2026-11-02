# 개발 담당: 최준혁
"""Synthetic, deterministic policy probes: no network, no orders, no key required."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from .config import Settings
from .economic_news import economic_indicators
from .decision_policy import decide


def demo():
    settings = Settings(_env_file=None)
    now = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)
    aliases = ["삼성전자", "Samsung Electronics"]
    def article(title, host, delay=0):
        return {"title": title, "description": "합성 시험 자료", "url": f"https://{host}/demo",
                "provider": "fixture", "published_at": (now-timedelta(hours=1)).isoformat(),
                "observed_at": now.isoformat(), "delay_hours": delay}
    positive = [article("삼성전자 실적 개선", "one.example"), article("삼성전자 공급계약 체결", "two.example")]
    negative = [article("삼성전자 적자 전환", "one.example"), article("삼성전자 공장 화재", "two.example")]
    uncertain = [article("삼성전자 실적 개선 전망", "one.example"), article("삼성전자 리콜 부인", "two.example")]
    cases = [("positive_support", .30, positive), ("negative_blocks_buy", .40, negative),
             ("negative_supports_sell", -.30, negative), ("uncertain_abstains", .30, uncertain),
             ("missing_news_keeps_baseline", .40, [])]
    rows = []
    for name, score, articles in cases:
        report = {"status": "ok", "symbol": "005930", "articles": articles}
        features = economic_indicators(report, "005930", aliases, now=now)
        decision = decide({"score": score, "data_ready": True}, {"availability": "disabled"}, features, settings)
        rows.append({"name": name, "decision": decision, "economic_news": features})
    return {"kind": "SYNTHETIC_POLICY_PROBES_NOT_MARKET_BACKTEST", "as_of": now.isoformat(),
            "technical_scores_are_test_inputs": True, "network_calls": 0, "orders": 0, "cases": rows}


def main():
    parser = argparse.ArgumentParser(description="뉴스 판단 규칙 합성 시연 (키/주문 없음)")
    parser.add_argument("--output", type=Path, default=Path("data/strategy_demo.json"))
    args = parser.parse_args()
    report = demo()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({r["name"]: {"before": r["decision"]["baseline_action"],
                                  "after": r["decision"]["action"]} for r in report["cases"]}))


if __name__ == "__main__":
    main()
