# 개발 담당: 최준혁
"""Controlled same-input policy ablation, not a return backtest."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .config import Settings
from .dart_analysis import validate_news
from .decision_policy import decide
from .economic_news import economic_indicators
from .news import parse_time

POLICY_FIELDS = ("decision_threshold", "news_max_adjustment", "news_min_sources",
                 "news_half_life_hours", "news_max_age_hours", "news_allow_delayed_in_strategy")


def policy_settings(settings):
    defaults = Settings(_env_file=None)
    return {field: getattr(settings, field, getattr(defaults, field)) for field in POLICY_FIELDS}


def compare_case(case, settings):
    snapshot = case.get("policy_settings", policy_settings(settings))
    if not isinstance(snapshot, dict) or set(snapshot) != set(POLICY_FIELDS):
        raise ValueError("invalid comparison policy settings")
    settings = Settings(_env_file=None, **snapshot)
    now = parse_time(case["as_of"])
    news = case.get("news")
    if news and news.get("symbol") != case["symbol"]:
        raise ValueError("comparison symbol mismatch")
    features = economic_indicators(news, case["symbol"], case["aliases"], now=now,
        half_life_hours=settings.news_half_life_hours, max_age_hours=settings.news_max_age_hours,
        allow_delayed=settings.news_allow_delayed_in_strategy, min_sources=settings.news_min_sources)
    validated = validate_news(features, case.get("dart"), now=now, min_sources=settings.news_min_sources)
    # LLM opinions can already incorporate news. Exclude them equally from all arms.
    disabled_llm = {"availability": "disabled"}
    enabled = settings.model_copy(update={"news_strategy_enabled": True})
    decisions = {
        "price_only": decide(case["technical"], disabled_llm, {}, enabled),
        "price_news": decide(case["technical"], disabled_llm, features, enabled),
        "price_news_dart": decide(case["technical"], disabled_llm, validated, enabled),
    }
    return {"id": case["id"], "symbol": case["symbol"], "as_of": case["as_of"], "decisions": decisions,
            "policy_settings": snapshot,
            "dart_validation": validated["dart_validation"], "event_count": sum(len(e["events"]) for e in features["evidence"]),
            "news_changed_action": decisions["price_only"]["action"] != decisions["price_news"]["action"],
            "dart_changed_action": decisions["price_news"]["action"] != decisions["price_news_dart"]["action"]}


def compare(dataset, settings=None):
    settings = settings or Settings(_env_file=None)
    rows, seen = [], set()
    for case in dataset["cases"]:
        if case["id"] in seen:
            raise ValueError("duplicate comparison case")
        seen.add(case["id"])
        rows.append(compare_case(case, settings))
    return {"kind": "SAME_INPUT_POLICY_ABLATION_NOT_RETURN_BACKTEST", "input_kind": dataset["kind"],
            "cases": rows, "case_count": len(rows), "network_calls": 0, "llm_calls": 0, "orders": 0,
            "action_counts": {arm: dict(Counter(r["decisions"][arm]["action"] for r in rows))
                              for arm in ("price_only", "price_news", "price_news_dart")},
            "news_changed_actions": sum(r["news_changed_action"] for r in rows),
            "dart_changed_actions": sum(r["dart_changed_action"] for r in rows),
            "limitations": "Identical recorded price/news/cutoff inputs; LLM disabled in all arms. Action differences do not measure returns, execution quality or causality. Current DART cannot reconstruct pre-correction historical statements."}


def load_recorded_inputs(path):
    # Read-only URI: a misspelled path must not create a new empty trading database.
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        cases = []
        for (raw,) in db.execute("SELECT report FROM policy_decisions ORDER BY id"):
            report = json.loads(raw)
            if report.get("research_input"):
                cases.append(report["research_input"])
    return {"kind": "RECORDED_RUNTIME_INPUTS", "cases": cases}


def main():
    parser = argparse.ArgumentParser(description="동일 입력의 가격/뉴스/DART 판단 비교 (외부 호출·주문 없음)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", type=Path)
    source.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dataset = (load_recorded_inputs(args.database) if args.database else
               json.loads(args.dataset.read_text(encoding="utf-8")))
    result = compare(dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("case_count", "news_changed_actions", "dart_changed_actions", "action_counts")}))


if __name__ == "__main__":
    main()
