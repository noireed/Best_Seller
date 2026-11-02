import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.news_evaluation import evaluate
from app.news_explain import explain

NOW = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)


def test_evaluation_reports_errors_and_reproducible_confusion_counts():
    dataset = json.loads(Path("examples/economic_labels.json").read_text(encoding="utf-8"))
    result = evaluate(dataset)
    assert result["case_count"] == 24 and len(result["errors"]) == 2
    assert sum(sum(row.values()) for row in result["confusion_matrix"].values()) == 24
    assert result["accuracy"] == 22/24
    assert {row["id"] for row in result["errors"]} == {"p7", "n6"}
    assert result["dataset_kind"].startswith("SYNTHETIC")


def test_duplicate_labels_rejected():
    case = {"id": "a", "title": "삼성전자 실적 개선", "expected": "positive"}
    with pytest.raises(ValueError):
        evaluate({"as_of": NOW.isoformat(), "kind": "synthetic", "cases": [case, case]})


def test_article_removal_explains_source_threshold_effect_without_api_call():
    articles = [{"title": title, "description": "", "provider": "fixture", "url": "https://"+host+"/1",
                 "published_at": (NOW-timedelta(hours=1)).isoformat(), "observed_at": NOW.isoformat()}
                for title, host in [("삼성전자 실적 개선", "one.example"), ("삼성전자 공급계약 체결", "two.example")]]
    result = explain({"score": .3, "data_ready": True}, {"availability": "disabled"},
        {"symbol": "005930", "status": "ok", "articles": articles}, "005930", ["삼성전자"],
        Settings(_env_file=None), now=NOW)
    assert result["decision"]["action"] == "BUY"
    assert all(row["action_without_article"] == "HOLD" for row in result["removals"])
    assert result["network_calls"] == result["llm_calls"] == 0


def test_llm_context_not_claimed_as_recomputed_without_real_model_call():
    llm = {"availability": "ok", "confidence": .9, "score": .8, "news_context_used": True}
    result = explain({"score": .3, "data_ready": True}, llm, None, "005930", ["삼성전자"],
                     Settings(_env_file=None), now=NOW)
    assert result["removal_status"].startswith("unsupported") and not result["removals"]


@pytest.mark.parametrize("title,value,direction,basis", [
    ("Samsung Electronics sales fell 7 percent year-on-year", 7, "decrease", "year_on_year"),
    ("삼성전자 매출 20% 증가", 20, "increase", "unspecified")])
def test_reported_percentage_keeps_metric_unit_and_comparison_basis(title, value, direction, basis):
    from app.economic_news import economic_indicators
    article = {"title":title, "url":"https://example.org/a", "provider":"fixture",
               "published_at":(NOW-timedelta(hours=1)).isoformat(), "observed_at":NOW.isoformat()}
    result = economic_indicators({"symbol":"005930","status":"ok","articles":[article]},
                                 "005930", ["Samsung Electronics","삼성전자"], now=NOW)
    change = result["evidence"][0]["events"][0]["reported_change"]
    assert change["metric"] == "revenue" and change["unit"] == "percent"
    assert (change["value"],change["direction"],change["comparison_basis"]) == (value,direction,basis)


def test_unbounded_numeric_text_cannot_generate_non_finite_economic_records():
    from app.economic_news import economic_indicators
    article = {"title":"Samsung Electronics revenue jumped " + "9"*400 + " percent",
               "url":"https://example.org/a", "provider":"fixture",
               "published_at":(NOW-timedelta(hours=1)).isoformat(), "observed_at":NOW.isoformat()}
    result = economic_indicators({"symbol":"005930","status":"ok","articles":[article]},
                                 "005930", ["Samsung Electronics"], now=NOW)
    assert result["excluded"][0]["reason"] == "non_finite_reported_change"
    assert not result["contribution_allowed"]
    json.dumps(result, allow_nan=False)
