# 개발 담당: 최준혁
"""Evaluate supplied labels. No network, training, returns or generalization claims."""
import argparse
from datetime import timedelta
import json
from pathlib import Path

from .config import Settings
from .economic_news import economic_indicators
from .news import parse_time
from .news_explain import explain

LABELS = ("positive", "negative", "neutral", "irrelevant", "excluded")


def classify(features):
    if features["evidence"]:
        impact = features["evidence"][0]["impact"]
        return "positive" if impact > 0 else "negative" if impact < 0 else "neutral"
    reasons = [row["reason"] for row in features["excluded"]]
    if any("company" in r or "subsidiary" in r or "preferred_share" in r for r in reasons):
        return "irrelevant"
    if reasons == ["no_actionable_economic_event"]:
        return "neutral"
    return "excluded"


def evaluate(dataset):
    cutoff = parse_time(dataset["as_of"])
    confusion = {label: {other: 0 for other in LABELS} for label in LABELS}
    rows, identities = [], set()
    for case in dataset["cases"]:
        if case["id"] in identities or case["expected"] not in LABELS:
            raise ValueError("duplicate case ID or invalid label")
        identities.add(case["id"])
        article = {"title": case["title"], "description": "", "provider": "fixture",
            "url": "https://evaluation.example/"+case["id"],
            "published_at": (cutoff-timedelta(hours=case.get("age_hours", 1))).isoformat(),
            "observed_at": cutoff.isoformat(), "delay_hours": 0}
        features = economic_indicators({"symbol": "005930", "status": "ok", "articles": [article]},
                                      "005930", ["삼성전자", "Samsung Electronics", "Samsung"], now=cutoff)
        predicted = classify(features)
        confusion[case["expected"]][predicted] += 1
        rows.append({"id": case["id"], "title": case["title"], "expected": case["expected"],
                     "predicted": predicted, "correct": predicted == case["expected"]})
    metrics = {}
    for label in LABELS:
        tp = confusion[label][label]
        predicted_count = sum(confusion[expected][label] for expected in LABELS)
        support = sum(confusion[label].values())
        precision = tp/predicted_count if predicted_count else 0.0
        recall = tp/support if support else 0.0
        metrics[label] = {"precision": precision, "recall": recall,
                          "f1": 2*precision*recall/(precision+recall) if precision+recall else 0.0,
                          "support": support}
    supported = [m for m in metrics.values() if m["support"]]
    return {"kind": "LABELED_RULE_EVALUATION_NOT_INVESTMENT_PERFORMANCE",
            "dataset_kind": dataset["kind"], "case_count": len(rows),
            "accuracy": sum(r["correct"] for r in rows)/len(rows) if rows else None,
            "macro_f1_supported_labels": sum(m["f1"] for m in supported)/len(supported) if supported else None,
            "metrics": metrics, "confusion_matrix": confusion, "cases": rows,
            "errors": [r for r in rows if not r["correct"]],
            "limitations": "Labels are supplied data; synthetic examples do not measure real-news generalization."}


def main():
    parser = argparse.ArgumentParser(description="경제사건 분류 평가 및 근거 제거 시연 (외부 호출 없음)")
    parser.add_argument("--dataset", type=Path, default=Path("examples/economic_labels.json"))
    parser.add_argument("--output", type=Path, default=Path("data/economic_evaluation.json"))
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    result = evaluate(dataset)
    cutoff = parse_time(dataset["as_of"])
    report = {"symbol": "005930", "status": "ok", "articles": [
        {"title": title, "description": "합성 시연", "provider": "fixture", "url": "https://"+host+"/demo",
         "published_at": (cutoff-timedelta(hours=1)).isoformat(), "observed_at": cutoff.isoformat(), "delay_hours": 0}
        for title, host in [("삼성전자 실적 개선", "one.example"), ("삼성전자 공급계약 체결", "two.example")]]}
    result["synthetic_sensitivity_demo"] = explain({"score": .3, "data_ready": True}, {"availability": "disabled"},
        report, "005930", ["삼성전자"], Settings(_env_file=None), now=cutoff)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({k:result[k] for k in ("dataset_kind", "case_count", "accuracy", "macro_f1_supported_labels")}))


if __name__ == "__main__":
    main()
