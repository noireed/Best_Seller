# 개발 담당: 최준혁
"""Blind annotation queues and strict event/number evaluation, without API calls."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from .event_extraction import extract_events, RULES
from .news import parse_time

STATES = {"reported", "forecast", "denied", "withdrawn", "uncertain", "mixed", "none"}
EVENTS = {rule[0] for rule in RULES}
FIGURE_FIELDS = ("metric", "value", "unit", "direction", "comparison_basis")


def prepare_queue(reports, aliases):
    cases, seen = [], set()
    for report in reports:
        symbol = report["symbol"]
        names = aliases.get(symbol, [])
        if not names:
            raise ValueError("aliases required for each symbol")
        for article in report.get("articles", []):
            if article.get("provider") != "gnews":
                raise ValueError("real-news queue accepts GNews records only")
            title, url = article["title"], article["url"]
            parsed = urlsplit(url)
            if not title.strip() or parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("invalid article provenance")
            if parse_time(article["published_at"]) > parse_time(article["observed_at"]):
                raise ValueError("article observed before publication")
            # Same title or URL for the same company cannot inflate evaluation support.
            title_key, url_key = (symbol, title.casefold()), (symbol, url)
            if title_key in seen or url_key in seen:
                continue
            seen.update((title_key, url_key))
            identity = hashlib.sha256((symbol+url).encode()).hexdigest()[:16]
            cases.append({"id": identity, "symbol": symbol, "aliases": names,
                **{key: article[key] for key in ("title", "url", "provider", "published_at", "observed_at")},
                "split": "unassigned", "annotator": "", "expected": None})
    return {"schema_version": 1, "kind": "REAL_NEWS_ANNOTATION_QUEUE", "cases": cases,
            "instructions": "Read the title only. Fill expected events/assertion/figures independently, identify the reviewer and assign development or holdout. No automatic gold labels or predictions are included."}


def _figure_key(figure):
    if not isinstance(figure, dict) or set(figure) != set(FIGURE_FIELDS):
        raise ValueError("figure requires metric/value/unit/direction/comparison_basis only")
    value = figure["value"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < float("inf"):
        raise ValueError("invalid numeric gold value")
    if (figure["metric"] not in {"revenue", "operating_profit"} or figure["unit"] != "percent" or
        figure["direction"] not in {"increase", "decrease"} or
        figure["comparison_basis"] not in {"year_on_year", "quarter_on_quarter", "unspecified", "ambiguous"}):
        raise ValueError("invalid figure label")
    return tuple(figure[key] for key in FIGURE_FIELDS)


def _counts(expected, predicted):
    a, b = Counter(expected), Counter(predicted)
    return sum((a & b).values()), sum((b-a).values()), sum((a-b).values())


def _metric(tp, fp, fn):
    precision = tp/(tp+fp) if tp+fp else 0.0
    recall = tp/(tp+fn) if tp+fn else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall,
            "f1": 2*precision*recall/(precision+recall) if precision+recall else 0.0}


def evaluate(dataset, *, split="holdout"):
    if dataset.get("kind") not in {"REAL_NEWS_ANNOTATION_QUEUE", "SYNTHETIC_EVENT_DIAGNOSTICS"}:
        raise ValueError("unknown dataset kind")
    if split not in {"development", "holdout"}:
        raise ValueError("evaluation split required")
    rows, seen_ids, seen_urls, seen_titles = [], set(), set(), set()
    totals = {"events": [0, 0, 0], "figures": [0, 0, 0]}
    confusion = {state: {other: 0 for other in sorted(STATES)} for state in sorted(STATES)}
    pending = 0
    for case in dataset["cases"]:
        if case["id"] in seen_ids:
            raise ValueError("duplicate case ID")
        seen_ids.add(case["id"])
        # Reject identical documents crossing split boundaries, not merely in the selected split.
        url_key, title_key = (case["symbol"], case["url"]), (case["symbol"], case["title"].casefold())
        if url_key in seen_urls or title_key in seen_titles:
            raise ValueError("duplicate article or cross-split leakage")
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        gold = case.get("expected")
        if gold is None:
            pending += 1
            continue
        if case.get("split") not in {"development", "holdout"} or not isinstance(case.get("annotator"), str) or not case["annotator"].strip():
            raise ValueError("completed labels require annotator and split")
        if case["split"] != split:
            continue
        if dataset["kind"] == "REAL_NEWS_ANNOTATION_QUEUE":
            parsed = urlsplit(case["url"])
            if case.get("provider") != "gnews" or parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("missing real-news provenance")
            if parse_time(case["published_at"]) > parse_time(case["observed_at"]):
                raise ValueError("invalid provenance timestamps")
        if (not isinstance(gold, dict) or set(gold) != {"events", "assertion", "figures"} or
            not isinstance(gold["events"], list) or any(e not in EVENTS for e in gold["events"]) or
            len(set(gold["events"])) != len(gold["events"]) or gold["assertion"] not in STATES or not isinstance(gold["figures"], list)):
            raise ValueError("invalid event gold labels")
        expected_figures = [_figure_key(f) for f in gold["figures"]]
        extracted = extract_events(case["title"], case["aliases"])
        predicted = {"events": sorted({e["event"] for e in extracted["events"]}), "assertion": extracted["assertion"],
            "figures": [{key: e["reported_change"][key] for key in FIGURE_FIELDS}
                        for e in extracted["events"] if "reported_change" in e]}
        for name, expected, actual in (("events", gold["events"], predicted["events"]),
            ("figures", expected_figures, [_figure_key(f) for f in predicted["figures"]])):
            counts = _counts(expected, actual)
            totals[name] = [x+y for x, y in zip(totals[name], counts)]
        confusion[gold["assertion"]][predicted["assertion"]] += 1
        correct = (set(gold["events"]) == set(predicted["events"]) and
                   gold["assertion"] == predicted["assertion"] and
                   Counter(expected_figures) == Counter(_figure_key(f) for f in predicted["figures"]))
        rows.append({"id": case["id"], "url": case["url"], "expected": gold, "predicted": predicted, "correct": correct})
    return {"kind": "EVENT_EXTRACTION_EVALUATION_NOT_INVESTMENT_PERFORMANCE", "dataset_kind": dataset["kind"],
        "split": split, "case_count": len(rows), "pending_annotation_count": pending,
        "status": "evaluated" if rows else "awaiting_labels",
        "exact_match_accuracy": sum(row["correct"] for row in rows)/len(rows) if rows else None,
        "events_micro": _metric(*totals["events"]) if rows else None,
        "figures_micro": _metric(*totals["figures"]) if rows else None,
        "assertion_confusion_matrix": confusion, "cases": rows, "errors": [r for r in rows if not r["correct"]],
        "limitations": "Split names and reviewer identities are supplied metadata, not independent certification. Exact deduplication cannot detect all syndicated or paraphrased articles. Development and synthetic scores do not establish real-news generalization."}


def main():
    parser = argparse.ArgumentParser(description="실제 뉴스 정답 검토 목록 생성 및 경제사건 평가 (주문 없음)")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--reports", type=Path, nargs="+", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    grading = sub.add_parser("evaluate")
    grading.add_argument("--dataset", type=Path, required=True)
    grading.add_argument("--split", choices=["development", "holdout"], default="holdout")
    grading.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        from .config import get_settings
        from .news import load_aliases
        settings = get_settings()
        aliases = load_aliases(settings.gnews_symbol_aliases)
        for symbol, names in load_aliases(settings.news_symbol_aliases).items():
            aliases.setdefault(symbol, []).extend(names)
        result = prepare_queue([json.loads(p.read_text(encoding="utf-8")) for p in args.reports], aliases)
    else:
        result = evaluate(json.loads(args.dataset.read_text(encoding="utf-8")), split=args.split)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("kind", "status", "case_count", "pending_annotation_count") if key in result}))


if __name__ == "__main__":
    main()
