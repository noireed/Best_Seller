from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import sqlite3
from unittest.mock import Mock, patch
import zipfile

import pytest

from app.config import Settings
from app.dart import DartClient
from app.dart_analysis import financial_metrics
from app.disclosure_reader import parse_document
from app.economic_news import economic_indicators
from app.engine import TradingEngine
from app.event_extraction import extract_events
from app.event_history import EventHistory, apply_history
from app.fundamental_analysis import apply_fundamentals, review_financials
from app.storage import Store

NOW = datetime(2026, 11, 5, 3, tzinfo=timezone.utc)
ALIASES = ["삼성전자", "Samsung Electronics", "Samsung"]
TITLE = "Samsung Electronics consolidated Q3 2026 revenue grew 10% YoY"


@pytest.mark.parametrize("title", [
    "삼성전자 협력사 영업이익 20% 증가", "삼성전자의 자회사 매출 10% 증가",
    "삼성전자 경쟁사 실적 개선", "삼성전자 고객사 매출 10% 증가",
    "Samsung Electronics supplier operating profit rose 20%", "Supplier of Samsung Electronics revenue grew 10%",
    "Samsung Display revenue grew 10%", "Samsung Electronics preferred revenue grew 10%",
    "삼성전자, 애플 영업이익 20% 증가", "Apple wins contract from Samsung Electronics",
    "삼성전자, 다른 기업 매출 10% 증가", "삼성전자와 애플 영업이익 20% 증가",
])
def test_related_company_or_object_cannot_become_target_event(title):
    result = extract_events(title, ALIASES)
    assert not result["events"] and result["rejected_events"]
    assert all(e["attribution"]["status"] != "direct" for e in result["rejected_events"])


@pytest.mark.parametrize("title", ["삼성전자 영업이익 20% 증가", TITLE,
    "Samsung Electronics wins supply contract with Apple", "Samsung Electronics wins contract with Samsung Display"])
def test_target_as_subject_with_counterparty_is_direct(title):
    result = extract_events(title, ALIASES)
    assert result["events"] and all(e["attribution"]["status"] == "direct" for e in result["events"])


def test_attribution_mentions_are_absolute_title_spans():
    title = "다른 회사 신제품; 삼성전자 영업이익 20% 증가"
    event = extract_events(title, ALIASES)["events"][0]
    for mention in event["attribution"]["target_mentions"]:
        assert title[slice(*mention["span"])] == mention["name"]


def article(title=TITLE, host="one.example", age=1, observed=NOW, path="a"):
    return {"title": title, "url": f"https://{host}/{path}", "provider": "fixture", "delay_hours": 0,
            "published_at": (NOW-timedelta(hours=age)).isoformat(), "observed_at": observed.isoformat()}


def report(*rows):
    return {"status": "ok", "symbol": "005930", "as_of": NOW.isoformat(), "articles": list(rows)}


def features(*rows):
    return economic_indicators(report(*rows), "005930", ALIASES, now=NOW)


def history(tmp_path):
    db = sqlite3.connect(tmp_path / "history.db")
    return db, EventHistory(db)


def test_history_persists_deduplicates_and_excludes_future_observations(tmp_path):
    db, service = history(tmp_path)
    service.ingest(report(article()), ALIASES, now=NOW)
    service.ingest(report(article()), ALIASES, now=NOW)
    assert db.execute("SELECT count(*) FROM economic_event_history").fetchone()[0] == 1
    db.close()
    db = sqlite3.connect(tmp_path / "history.db")
    reopened = EventHistory(db)
    assert len(reopened.snapshot("005930", now=NOW)["groups"]) == 1
    assert not reopened.snapshot("005930", now=NOW-timedelta(hours=2))["groups"]
    db.close()


def test_same_period_different_values_are_conflicting_not_extra_votes(tmp_path):
    db, service = history(tmp_path)
    rows = [article(), article(TITLE.replace("10%", "30%"), "two.example", age=.5)]
    service.ingest(report(*rows), ALIASES, now=NOW)
    snapshot = service.snapshot("005930", now=NOW)
    assert len(snapshot["groups"]) == 1 and snapshot["groups"][0]["state"] == "conflicting"
    result = apply_history(features(*rows), snapshot, now=NOW)
    assert not result["contribution_allowed"] and result["score"] == 0
    db.close()


def test_denial_and_correction_preserve_original_but_remove_its_vote(tmp_path):
    db, service = history(tmp_path)
    original = article(age=2)
    denial = article("Samsung Electronics denies consolidated Q3 2026 revenue grew 10% YoY", "two.example", age=1)
    service.ingest(report(original, denial), ALIASES, now=NOW)
    snapshot = service.snapshot("005930", now=NOW)
    assert snapshot["groups"][0]["suppressed_urls"][original["url"]] == "later_denial_or_withdrawal"
    assert len(snapshot["groups"][0]["observations"]) == 2
    assert apply_history(features(original), snapshot, now=NOW)["evidence"][0]["impact"] == 0
    # A correction from the same publisher replaces that publisher's earlier value.
    db.execute("DELETE FROM economic_event_history")
    correction = article("Correction: "+TITLE.replace("10%", "8%"), age=.5, path="correction")
    service.ingest(report(original, correction), ALIASES, now=NOW)
    snapshot = service.snapshot("005930", now=NOW)
    assert snapshot["groups"][0]["state"] == "revised"
    assert snapshot["groups"][0]["suppressed_urls"][original["url"]] == "same_publisher_correction"
    assert correction["url"] not in snapshot["groups"][0]["suppressed_urls"]
    db.close()


def test_different_quarters_or_unspecified_contracts_are_not_merged(tmp_path):
    db, service = history(tmp_path)
    service.ingest(report(article(), article(TITLE.replace("Q3", "Q2"), "two.example"),
        article("삼성전자 공급계약 체결", "three.example"), article("삼성전자 공급계약 체결", "four.example")), ALIASES, now=NOW)
    snapshot = service.snapshot("005930", now=NOW)
    assert len(snapshot["groups"]) == 4
    assert sum(g["linkage"] == "same_article_only" for g in snapshot["groups"]) == 2
    db.close()


def test_repeated_financial_claim_keeps_original_age_and_expires(tmp_path):
    db, service = history(tmp_path)
    old = article(age=47, observed=NOW-timedelta(hours=46))
    service.ingest(report(old), ALIASES, now=NOW-timedelta(hours=46))
    new = article(TITLE, "two.example", age=.5)
    service.ingest(report(new), ALIASES, now=NOW)
    result = apply_history(features(new), service.snapshot("005930", now=NOW), now=NOW)
    assert result["evidence"][0]["weight"] < .1
    assert result["event_history"]["recycled_urls"] == [new["url"]]
    later = NOW+timedelta(hours=3)
    new_features = economic_indicators(report(new), "005930", ALIASES, now=later)
    expired = apply_history(new_features, service.snapshot("005930", now=later), now=later)
    assert expired["evidence"][0]["impact"] == 0
    db.close()


def zip_document(text):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("report.xml", text)
    return buffer.getvalue()


@pytest.mark.parametrize("text", [
    '<DOCUMENT><TITLE>사업의 내용</TITLE><P>회사는 반도체와 전자제품의 연구개발 및 생산 판매 사업을 영위하고 있습니다.</P></DOCUMENT>',
    '<DOCUMENT><TITLE>사업의 내용</TITLE><P>회사는 R&D 역량을 활용하여 반도체와 전자제품의 생산 판매 사업을 영위하고 있습니다.</P></DOCUMENT>',
])
def test_original_document_parsing_preserves_source_and_handles_bare_ampersand(text):
    parsed = parse_document(zip_document(text), "20261104000001")
    assert parsed["status"] == "ok" and len(parsed["excerpts"]) == 1
    assert parsed["receipt"] == "20261104000001" and parsed["archive_sha256"]
    assert parsed["excerpts"][0]["topics"] == ["business"]
    if "R&D" in text:
        assert "R&D" in parsed["excerpts"][0]["text"] and parsed["parse_modes"] == ["tolerant_markup"]


def test_document_entity_declarations_are_rejected():
    with pytest.raises(ValueError, match="entities"):
        parse_document(zip_document('<!DOCTYPE x [<!ENTITY x "bad">]><DOCUMENT/>'), "20261104000001")


def test_document_parser_error_does_not_cooldown_structured_financial_requests(tmp_path):
    session = Mock()
    session.get.return_value = Mock(status_code=200, content=zip_document("<DOCUMENT/>"))
    client = DartClient("test", tmp_path / "cache.db", session=session, clock=lambda: NOW)
    with patch("app.disclosure_reader.parse_document", side_effect=ValueError("unsupported")):
        with pytest.raises(Exception, match="DOCUMENT_FORMAT"):
            client._request("document.xml", {"rcept_no": "20261104000001"})
    session.get.return_value = Mock(status_code=200, json=Mock(return_value={"status": "013"}))
    assert client._request("list.json", {})[0]["status"] == "013"


def financials():
    common = {"sj_div": "IS", "currency": "KRW"}
    rows = [{**common, "account_id": "ifrs-full_Revenue", "thstrm_amount": "110", "frmtrm_q_amount": "100"},
            {**common, "account_id": "dart_OperatingIncomeLoss", "thstrm_amount": "5", "frmtrm_q_amount": "10"}]
    result = financial_metrics(rows, 2026, 9, "20261104000001")
    result.update(observed_at=NOW.isoformat(), filing_date="20261104", url="https://dart.fss.or.kr/example")
    return result


def test_profit_decline_attenuates_only_comparable_positive_news():
    data = financials()
    assert "revenue_growth_with_profit_decline" in review_financials(data)["cautions"]
    original = features(article())
    checked = apply_fundamentals(original, {"status": "ok", "symbol": "005930", "financials": data}, now=NOW)
    assert checked["evidence"][0]["impact"] == original["evidence"][0]["impact"]*.5
    assert checked["fundamental_guard"]["adjusted_urls"]
    assert original["evidence"][0]["impact"] == .5


@pytest.mark.parametrize("title", [TITLE.replace("Q3", "Q2"), TITLE.replace("consolidated ", ""),
    "Samsung Electronics consolidated Q3 2026 wins supply contract", TITLE.replace("YoY", "QoQ")])
def test_financial_guard_never_applies_across_period_or_metric_or_basis(title):
    original = features(article(title))
    checked = apply_fundamentals(original, {"status": "ok", "symbol": "005930", "financials": financials()}, now=NOW)
    assert not checked["fundamental_guard"]["adjusted_urls"]
    assert [r["impact"] for r in checked["evidence"]] == [r["impact"] for r in original["evidence"]]


def test_balance_ratios_and_cumulative_cash_flow_are_explicit():
    rows = [{"sj_div": "BS", "currency": "KRW", "account_id": "ifrs-full_"+account, "thstrm_amount": amount}
            for account, amount in [("CurrentAssets", "80"), ("CurrentLiabilities", "100"), ("Liabilities", "200"), ("Equity", "100")]]
    rows.append({"sj_div": "CF", "currency": "KRW", "account_id": "ifrs-full_CashFlowsFromUsedInOperatingActivities", "thstrm_amount": "-5"})
    parsed = financial_metrics(rows, 2026, 9, "20261104000001")
    assert parsed["balance_and_cash_flow"]["current_ratio"] == .8
    assert parsed["balance_and_cash_flow"]["liabilities_to_equity_ratio"] == 2
    assert parsed["balance_and_cash_flow"]["cash_flow_period"] == "year_to_date"
    assert "negative_operating_cash_flow_ytd" in review_financials(parsed)["cautions"]


def test_engine_keeps_related_company_out_of_llm_and_records_history(tmp_path):
    settings = Settings(_env_file=None, watchlist="005930", news_use_in_llm=True)
    api, llm, news = Mock(), Mock(), Mock()
    api.portfolio.return_value = {"availableCash": 100000000, "totalAssetValue": 100000000, "holdings": []}
    api.limits.return_value = {}
    api.prices.return_value = {"result": [{"symbol": "005930", "price": 70000}]}
    api.candles.return_value = {"candles": []}
    llm.assess.return_value = {"availability": "disabled"}
    news.collect.return_value = report(article("삼성전자 협력사 영업이익 20% 증가"))
    store = Store(str(tmp_path / "store.db"))
    with patch("app.engine.technical_signal", return_value={"score": .3, "data_ready": True}):
        result = TradingEngine(settings, api, llm, store, news, clock=lambda: NOW).run_cycle(scan_only=True)[0]
    assert result["action"] == "HOLD" and "news" not in llm.assess.call_args.kwargs
    assert result["economic_news"]["excluded"]
    api.place_order.assert_not_called()
    store.db.close()


def test_reprinting_a_denied_value_does_not_revive_it(tmp_path):
    db, service = history(tmp_path)
    original = article(age=4)
    denial = article("Samsung Electronics denies consolidated Q3 2026 revenue grew 10% YoY", "two.example", age=2)
    reprint = article(TITLE, "three.example", age=.5)
    service.ingest(report(original, denial, reprint), ALIASES, now=NOW)
    snapshot = service.snapshot("005930", now=NOW)
    assert reprint["url"] in snapshot["groups"][0]["suppressed_urls"]
    assert apply_history(features(reprint), snapshot, now=NOW)["evidence"][0]["impact"] == 0
    db.close()


def test_snapshot_cutoff_is_equivalent_in_different_timezones(tmp_path):
    db, service = history(tmp_path)
    service.ingest(report(article()), ALIASES, now=NOW)
    utc = service.snapshot("005930", now=NOW)
    other_zone = service.snapshot("005930", now=NOW.astimezone(timezone(timedelta(hours=-9))))
    assert utc == other_zone
    db.close()


def test_company_history_fixture_preserves_chronology_and_corrections():
    from pathlib import Path
    from app.company_cli import research_fixture
    dataset = json.loads(Path("examples/company_event_history.json").read_text(encoding="utf-8"))
    result = research_fixture(dataset)
    assert [row["event_history"]["groups"][0]["state"] for row in result["snapshots"]] == ["observed", "conflicting", "revised"]
    assert result["network_calls"] == result["orders"] == 0
    assert len(result["snapshots"][-1]["event_history"]["groups"][0]["observations"]) == 4


@pytest.mark.parametrize("kind", ["future", "stale", "disabled"])
def test_fundamental_guard_never_uses_unavailable_evidence(kind):
    data = financials()
    if kind == "future":
        data["observed_at"] = (NOW+timedelta(seconds=1)).isoformat()
    elif kind == "stale":
        data["observed_at"] = (NOW-timedelta(days=2)).isoformat()
    result = apply_fundamentals(features(article()), {"status": "ok", "symbol": "005930", "financials": data},
                                now=NOW, enabled=kind != "disabled")
    assert not result["fundamental_guard"]["adjusted_urls"]


def test_financial_attenuation_cannot_be_bypassed_through_llm_news(tmp_path):
    settings = Settings(_env_file=None, watchlist="005930", news_use_in_llm=True)
    api, llm, news, dart = Mock(), Mock(), Mock(), Mock()
    api.portfolio.return_value = {"availableCash": 100000000, "totalAssetValue": 100000000, "holdings": []}
    api.limits.return_value = {}
    api.prices.return_value = {"result": [{"symbol": "005930", "price": 70000}]}
    api.candles.return_value = {"candles": []}
    llm.assess.return_value = {"availability": "disabled"}
    news.collect.return_value = report(article())
    dart.collect.return_value = {"status": "ok", "symbol": "005930", "financials": financials()}
    store = Store(str(tmp_path / "store.db"))
    with patch("app.engine.technical_signal", return_value={"score": .3, "data_ready": True}):
        result = TradingEngine(settings, api, llm, store, news, clock=lambda: NOW, dart=dart).run_cycle(scan_only=True)[0]
    assert result["economic_news"]["fundamental_guard"]["adjusted_urls"]
    assert "news" not in llm.assess.call_args.kwargs
    assert result["research_input"]["event_history"]["groups"]
    assert result["policy_comparison"]["decisions"]["price_news_dart"]["action"] == "HOLD"
    api.place_order.assert_not_called()
    store.db.close()
