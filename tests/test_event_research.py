from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock, patch

import pytest

from app.config import Settings
from app.economic_news import economic_indicators
from app.engine import TradingEngine
from app.event_evaluation import evaluate, prepare_queue
from app.event_extraction import extract_events
from app.policy_comparison import compare, load_recorded_inputs
from app.storage import Store

NOW = datetime(2026, 11, 5, 3, tzinfo=timezone.utc)
ALIASES = ["삼성전자", "Samsung Electronics"]


@pytest.mark.parametrize("title,state,event", [
    ("삼성전자 영업이익 20% 증가", "reported", "earnings_improvement"),
    ("삼성전자 영업이익 20% 증가 전망", "forecast", "earnings_improvement"),
    ("삼성전자 영업이익 20% 증가 전망 철회", "withdrawn", "earnings_improvement"),
    ("삼성전자 영업이익 20% 증가 보도 부인", "denied", "earnings_improvement"),
    ("Samsung Electronics operating profit rose 20 percent YoY", "reported", "earnings_improvement"),
    ("Samsung Electronics operating profit fell 20% QoQ", "reported", "earnings_deterioration"),
    ("Samsung Electronics revenue is expected to grow 10% YoY", "forecast", "revenue_increase"),
    ("Samsung Electronics revenue will fall 10%", "forecast", "revenue_decrease"),
    ("Samsung Electronics withdraws profit growth forecast", "withdrawn", "earnings_improvement"),
    ("Samsung Electronics denies factory fire", "denied", "operating_risk"),
    ("Samsung Electronics may announce share buyback", "uncertain", "capital_return"),
    ("삼성전자 신제품 공개", "none", None),
    ("경쟁기업 영업이익 20% 증가", "none", None),
])
def test_assertion_entity_event_and_exact_evidence_span(title, state, event):
    result = extract_events(title, ALIASES)
    assert result["assertion"] == state
    assert {e["event"] for e in result["events"]} == ({event} if event else set())
    for row in result["events"]:
        start, end = row["span"]
        assert title[start:end] == row["evidence_text"]
        assert row["actor"] in ALIASES


@pytest.mark.parametrize("title,metric,value,direction,basis", [
    ("삼성전자 전년 대비 매출 12.5% 증가", "revenue", 12.5, "increase", "year_on_year"),
    ("삼성전자 영업이익 20% 감소", "operating_profit", 20, "decrease", "unspecified"),
    ("Samsung Electronics operating profit fell 7 percent quarter-on-quarter", "operating_profit", 7, "decrease", "quarter_on_quarter"),
    ("Samsung Electronics revenue rose 10% YoY and QoQ", "revenue", 10, "increase", "ambiguous"),
    ("삼성전자 영업이익 전년 동기 대비 20% 증가", "operating_profit", 20, "increase", "year_on_year"),
    ("Samsung Electronics revenue up 12% from last year", "revenue", 12, "increase", "year_on_year"),
])
def test_numeric_features_preserve_metric_and_basis(title, metric, value, direction, basis):
    change = extract_events(title, ALIASES)["events"][0]["reported_change"]
    assert (change["metric"], change["value"], change["direction"], change["comparison_basis"]) == (metric, value, direction, basis)
    assert change["unit"] == "percent"


def article(title, host="one.example"):
    return {"title": title, "url": "https://"+host+"/a", "published_at": (NOW-timedelta(hours=1)).isoformat(),
            "observed_at": NOW.isoformat(), "provider": "gnews", "delay_hours": 0}


@pytest.mark.parametrize("suffix", [" 전망", " 보도 부인", " 전망 철회", " 가능성"])
def test_unconfirmed_operating_profit_never_creates_direct_score(suffix):
    report = {"symbol": "005930", "status": "ok", "articles": [
        article("삼성전자 영업이익 20% 증가"+suffix), article("삼성전자 매출 10% 증가"+suffix, "two.example")]}
    result = economic_indicators(report, "005930", ALIASES, now=NOW)
    assert result["evidence"] and all(row["impact"] == 0 for row in result["evidence"])
    assert not result["contribution_allowed"]


def test_other_company_clause_cannot_supply_target_event():
    result = extract_events("삼성전자 신제품 공개; 다른 기업 영업이익 20% 증가", ALIASES)
    assert result["events"] == []


def queue():
    return prepare_queue([{"symbol": "005930", "articles": [article("삼성전자 영업이익 20% 증가")]}], {"005930": ALIASES})


def annotated():
    result = queue()
    case = result["cases"][0]
    case.update(annotator="test reviewer", split="holdout", expected={"events": ["earnings_improvement"],
        "assertion": "reported", "figures": [{"metric": "operating_profit", "value": 20, "unit": "percent",
        "direction": "increase", "comparison_basis": "unspecified"}]})
    return result


def test_annotation_queue_is_blind_and_pending_is_not_scored():
    dataset = queue()
    assert dataset["cases"][0]["expected"] is None
    assert "predicted" not in json.dumps(dataset)
    result = evaluate(dataset)
    assert result["status"] == "awaiting_labels" and result["case_count"] == 0
    assert result["exact_match_accuracy"] is None and result["events_micro"] is None
    assert result["pending_annotation_count"] == 1


def test_real_queue_keeps_provenance_and_deduplicates():
    report = {"symbol": "005930", "articles": [article("삼성전자 매출 10% 증가")]*2}
    assert len(prepare_queue([report], {"005930": ALIASES})["cases"]) == 1
    report["articles"][0]["provider"] = "fixture"
    with pytest.raises(ValueError, match="GNews"):
        prepare_queue([report], {"005930": ALIASES})


def test_evaluation_micro_metrics_and_errors_are_real_counts():
    result = evaluate(annotated())
    assert result["case_count"] == 1 and result["events_micro"]["tp"] == 1
    assert result["figures_micro"]["f1"] == 1 and result["exact_match_accuracy"] == 1
    wrong = annotated()
    wrong["cases"][0]["expected"]["figures"][0]["value"] = 30
    result = evaluate(wrong)
    assert len(result["errors"]) == 1 and result["figures_micro"]["fp"] == 1 and result["figures_micro"]["fn"] == 1


@pytest.mark.parametrize("field,value", [("annotator", ""), ("split", "unassigned")])
def test_completed_labels_require_reviewer_and_split(field, value):
    data = annotated()
    data["cases"][0][field] = value
    with pytest.raises(ValueError):
        evaluate(data)


def test_duplicate_document_across_splits_is_rejected():
    data = annotated()
    duplicate = deepcopy(data["cases"][0])
    duplicate.update(id="other", split="development")
    data["cases"].append(duplicate)
    with pytest.raises(ValueError, match="leakage"):
        evaluate(data)


def test_development_labels_do_not_enter_holdout_score():
    data = annotated()
    data["cases"][0]["split"] = "development"
    assert evaluate(data)["case_count"] == 0
    assert evaluate(data, split="development")["case_count"] == 1


def test_comparison_uses_same_inputs_without_llm_or_orders():
    case = {"id": "a", "symbol": "005930", "as_of": NOW.isoformat(), "aliases": ALIASES,
            "technical": {"score": .30, "data_ready": True}, "news": {"symbol": "005930", "status": "ok", "articles": [
            article("삼성전자 영업이익 20% 증가"), article("삼성전자 공급계약 체결", "two.example")]}}
    original = deepcopy(case)
    result = compare({"kind": "SYNTHETIC", "cases": [case]})
    decisions = result["cases"][0]["decisions"]
    assert decisions["price_only"]["action"] == "HOLD" and decisions["price_news"]["action"] == "BUY"
    assert decisions["price_news_dart"]["action"] == "BUY"
    assert case == original and result["network_calls"] == result["orders"] == result["llm_calls"] == 0
    assert not any(d["llm_used"] for d in decisions.values())


def test_empty_comparison_makes_no_performance_claim():
    result = compare({"kind": "RECORDED", "cases": []})
    assert result["case_count"] == 0 and result["news_changed_actions"] == 0
    assert "return" not in result


def test_future_news_does_not_enter_comparison():
    report = {"symbol": "005930", "status": "ok", "articles": [article("삼성전자 영업이익 20% 증가")]}
    case = {"id": "a", "symbol": "005930", "as_of": (NOW-timedelta(days=1)).isoformat(),
            "aliases": ALIASES, "technical": {"score": .3, "data_ready": True}, "news": report}
    assert compare({"kind": "SYNTHETIC", "cases": [case]})["cases"][0]["event_count"] == 0


@pytest.mark.parametrize("title,event_count", [("삼성전자 영업이익 20% 증가 전망 철회", 1), ("삼성전자 매출 증가 전망", 0)])
def test_engine_filters_forecast_from_llm_and_exports_research_inputs(tmp_path, title, event_count):
    settings = Settings(_env_file=None, watchlist="005930", news_use_in_llm=True)
    api, llm, news = Mock(), Mock(), Mock()
    api.portfolio.return_value = {"availableCash": 100000000, "totalAssetValue": 100000000, "holdings": []}
    api.limits.return_value = {}
    api.prices.return_value = {"result": [{"symbol": "005930", "price": 70000}]}
    api.candles.return_value = {"candles": []}
    llm.assess.return_value = {"availability": "disabled"}
    news.collect.return_value = {"symbol": "005930", "status": "ok", "as_of": NOW.isoformat(),
                                "articles": [article(title)]}
    path = tmp_path / "audit.db"
    store = Store(str(path))
    with patch("app.engine.technical_signal", return_value={"score": .3, "data_ready": True}):
        result = TradingEngine(settings, api, llm, store, news, clock=lambda: NOW).run_cycle(scan_only=True)[0]
    assert "news" not in llm.assess.call_args.kwargs
    assert result["action"] == "HOLD" and result["policy_comparison"]["event_count"] == event_count
    saved = load_recorded_inputs(path)
    assert saved["kind"] == "RECORDED_RUNTIME_INPUTS" and len(saved["cases"]) == 1
    assert compare(saved)["case_count"] == 1
    api.place_order.assert_not_called()
    store.db.close()


def test_missing_comparison_database_is_not_created(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(Exception):
        load_recorded_inputs(path)
    assert not path.exists()


def test_comparison_replays_recorded_threshold_instead_of_new_defaults():
    from app.policy_comparison import policy_settings
    settings = Settings(_env_file=None, decision_threshold=.6)
    case = {"id": "snapshot", "symbol": "005930", "aliases": ALIASES, "as_of": NOW.isoformat(),
            "technical": {"score": .4, "data_ready": True}, "policy_settings": policy_settings(settings)}
    result = compare({"kind": "SYNTHETIC", "cases": [case]})
    assert result["cases"][0]["decisions"]["price_only"]["action"] == "HOLD"
    assert result["cases"][0]["policy_settings"]["decision_threshold"] == .6


def test_comparison_failure_does_not_discard_primary_decision(tmp_path):
    settings = Settings(_env_file=None, watchlist="005930")
    api, llm = Mock(), Mock()
    api.portfolio.return_value = {"availableCash": 100000000, "totalAssetValue": 100000000, "holdings": []}
    api.limits.return_value = {}
    api.prices.return_value = {"result": [{"symbol": "005930", "price": 70000}]}
    api.candles.return_value = {"candles": []}
    llm.assess.return_value = {"availability": "disabled"}
    store = Store(str(tmp_path / "audit.db"))
    with patch("app.engine.technical_signal", return_value={"score": .4, "data_ready": True}), \
         patch("app.engine.compare_case", side_effect=ValueError("research failure")):
        result = TradingEngine(settings, api, llm, store, clock=lambda: NOW).run_cycle(scan_only=True)[0]
    assert result["action"] == "BUY" and result["policy_comparison"]["status"] == "unavailable"
    api.place_order.assert_not_called()
    store.db.close()


def test_checked_in_examples_have_valid_gold_and_three_arm_results():
    from pathlib import Path
    diagnostics = json.loads(Path("examples/event_diagnostics.json").read_text(encoding="utf-8"))
    result = evaluate(diagnostics, split="development")
    assert result["case_count"] == 24 and result["dataset_kind"] == "SYNTHETIC_EVENT_DIAGNOSTICS"
    assert not result["errors"]
    dataset = json.loads(Path("examples/policy_comparison.json").read_text(encoding="utf-8"))
    compared = compare(dataset)
    assert compared["news_changed_actions"] == 2 and compared["dart_changed_actions"] == 1
    assert compared["orders"] == 0
