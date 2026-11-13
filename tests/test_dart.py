from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from unittest.mock import Mock, patch
import zipfile

import pytest
import requests

from app.config import Settings
from app.dart import DartClient, DartError, build_dart_service
from app.dart_analysis import financial_metrics, validate_news
from app.decision_policy import decide
from app.economic_news import economic_indicators
from app.engine import TradingEngine
from app.news_explain import explain
from app.storage import Store

NOW = datetime(2026, 11, 5, 3, tzinfo=timezone.utc)
RECEIPT = "20261104000001"
CORP = "00126380"
SYMBOL = "005930"


def accounts(year=2026, code="11014", receipt=RECEIPT):
    common = {"rcept_no": receipt, "corp_code": CORP, "bsns_year": str(year),
              "reprt_code": code, "sj_div": "IS", "currency": "KRW"}
    return [{**common, "account_id": "ifrs-full_Revenue", "account_nm": "매출액",
             "thstrm_amount": "110,000", "frmtrm_q_amount": "100,000", "frmtrm_amount": "900,000",
             "thstrm_add_amount": "330,000", "frmtrm_add_amount": "300,000"},
            {**common, "account_id": "dart_OperatingIncomeLoss", "account_nm": "영업이익",
             "thstrm_amount": "5,500", "frmtrm_q_amount": "10,000", "frmtrm_amount": "90,000",
             "thstrm_add_amount": "16,500", "frmtrm_add_amount": "30,000"}]


def dart_report(rows=None):
    metrics = financial_metrics(rows or accounts(), 2026, 9, RECEIPT)
    metrics.update(observed_at=NOW.isoformat(), filing_date="20261104", url="https://dart.fss.or.kr/example")
    return {"status": "ok", "symbol": SYMBOL, "financials": metrics}


def news(title="Samsung Electronics consolidated Q3 2026 revenue grew 10% YoY"):
    article = {"title": title, "url": "https://one.example/a", "provider": "fixture", "delay_hours": 0,
               "published_at": (NOW-timedelta(hours=1)).isoformat(), "observed_at": NOW.isoformat()}
    report = {"status": "ok", "symbol": SYMBOL, "as_of": NOW.isoformat(), "articles": [article,
        {**article, "title": "Samsung Electronics wins supply contract", "url": "https://two.example/b"}]}
    return report


def economics(report):
    return economic_indicators(report, SYMBOL, ["Samsung Electronics"], now=NOW)


def response(payload):
    return Mock(status_code=200, json=Mock(return_value=payload))


def corp_response():
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("CORPCODE.xml", f"<result><list><stock_code>{SYMBOL}</stock_code><corp_code>{CORP}</corp_code></list></result>")
    return Mock(status_code=200, content=content.getvalue())


def filings(receipt=RECEIPT):
    return {"status": "000", "total_page": 1, "list": [
        {"corp_code": CORP, "report_nm": "분기보고서 (2026.09)", "rcept_dt": "20261104", "rcept_no": receipt}]}


def client(tmp_path, session=None, **kwargs):
    return DartClient("test-key-do-not-log", tmp_path / "dart.db", session=session or Mock(), clock=lambda: NOW, **kwargs)


def test_quarter_and_cumulative_use_matching_prior_year_columns():
    metrics = financial_metrics(accounts(), 2026, 9, RECEIPT)
    assert metrics["metrics"]["revenue"]["yoy_pct"] == 10
    assert metrics["metrics"]["revenue"]["cumulative_yoy_pct"] == 10
    assert metrics["operating_margin_pct"] == 5
    assert metrics["prior_operating_margin_pct"] == 10
    assert metrics["operating_margin_change_pp"] == -5
    annual = financial_metrics(accounts(2025, "11011"), 2025, 12, RECEIPT)
    assert annual["period"] == "annual"
    assert annual["metrics"]["revenue"]["prior_year_same_period"] == 900000


@pytest.mark.parametrize("prior", ["0", "-100", "-", "", "NaN"])
def test_zero_negative_or_missing_bases_do_not_generate_growth(prior):
    rows = accounts()
    rows[0]["frmtrm_q_amount"] = prior
    assert financial_metrics(rows, 2026, 9, RECEIPT)["metrics"]["revenue"]["yoy_pct"] is None


def test_ambiguous_accounts_or_currency_are_not_summed():
    rows = accounts()
    rows.append(deepcopy(rows[0]))
    assert "revenue" not in financial_metrics(rows, 2026, 9, RECEIPT)["metrics"]
    rows = accounts()
    rows[0]["currency"] = "USD"
    assert "revenue" not in financial_metrics(rows, 2026, 9, RECEIPT)["metrics"]


def test_missing_key_never_calls_network(tmp_path):
    session = Mock()
    service = DartClient("", tmp_path / "dart.db", session=session, clock=lambda: NOW)
    assert service.collect(SYMBOL)["reason"] == "DART_API_KEY 미설정"
    session.get.assert_not_called()
    assert build_dart_service(Settings(_env_file=None, dart_enabled=False)) is None


def test_real_adapter_path_and_persistent_cache(tmp_path):
    session = Mock()
    session.get.side_effect = [corp_response(), response(filings()), response({"status": "000", "list": accounts()})]
    result = client(tmp_path, session).collect(SYMBOL)
    assert result["financial_status"] == "ok"
    assert result["financials"]["metrics"]["revenue"]["yoy_pct"] == 10
    assert session.get.call_count == 3
    restarted_session = Mock()
    repeated = client(tmp_path, restarted_session).collect(SYMBOL)
    assert repeated == result
    restarted_session.get.assert_not_called()
    assert session.get.call_args.kwargs["params"]["fs_div"] == "CFS"
    assert session.get.call_args_list[1].kwargs["params"]["pblntf_ty"] == "A"
    assert "test-key-do-not-log" not in json.dumps(result)
    assert b"test-key-do-not-log" not in (tmp_path / "dart.db").read_bytes()


@pytest.mark.parametrize("status", ["010", "020", "800", "untrusted-secret-text"])
def test_status_failures_cooldown_and_do_not_echo_provider_messages(tmp_path, status):
    session = Mock()
    session.get.return_value = response({"status": status, "message": "test-key-do-not-log"})
    service = client(tmp_path, session)
    with pytest.raises(DartError) as failure:
        service._request("list.json", {})
    assert "test-key-do-not-log" not in str(failure.value)
    assert "untrusted-secret-text" not in str(failure.value)
    assert service.collect(SYMBOL)["reason"] == "DART_COOLDOWN"
    assert session.get.call_count == 1


def test_timeout_sanitizes_url_and_expired_cache_is_not_reused(tmp_path):
    session = Mock()
    session.get.return_value = response({"status": "000", "list": []})
    service = client(tmp_path, session, ttl_seconds=60)
    service._request("list.json", {})
    service.clock = lambda: NOW+timedelta(seconds=61)
    session.get.side_effect = requests.Timeout("https://example.com?crtfc_key=test-key-do-not-log")
    with pytest.raises(DartError, match="^DART_TRANSPORT_OR_FORMAT_ERROR$"):
        service._request("list.json", {})


def test_local_budget_is_shared_across_restarts(tmp_path):
    session = Mock()
    session.get.return_value = response({"status": "013"})
    client(tmp_path, session, daily_budget=1)._request("list.json", {"page": 1})
    with pytest.raises(DartError, match="DART_LOCAL_DAILY_BUDGET"):
        client(tmp_path, session, daily_budget=1)._request("list.json", {"page": 2})
    assert session.get.call_count == 1


def test_correction_receipt_mismatch_is_unusable(tmp_path):
    session = Mock()
    session.get.side_effect = [corp_response(), response(filings()),
        response({"status": "000", "list": accounts(receipt="20261105000002")})]
    result = client(tmp_path, session).collect(SYMBOL)
    assert result["financials"] is None
    assert result["financial_status"] == "receipt_or_period_mismatch_or_no_data"


def test_newer_correction_of_older_period_does_not_replace_latest_period(tmp_path):
    payload = filings()
    payload["list"].append({"corp_code": CORP, "report_nm": "[기재정정]사업보고서 (2025.12)",
                            "rcept_dt": "20261105", "rcept_no": "20261105000002"})
    session = Mock()
    session.get.side_effect = [corp_response(), response(payload), response({"status": "000", "list": accounts()})]
    assert client(tmp_path, session).collect(SYMBOL)["financials"]["quarter"] == 3


def test_consistent_claim_is_annotated_without_increasing_score():
    before = economics(news())
    after = validate_news(before, dart_report(), now=NOW)
    assert after["dart_validation"]["checks"][0]["status"] == "consistent"
    assert before["score"] == after["score"]
    assert "dart_validation" not in before


def test_contradiction_removes_vote_and_recomputes_source_gate():
    before = economics(news("Samsung Electronics consolidated Q3 2026 revenue grew 30% YoY"))
    assert before["contribution_allowed"]
    assert decide({"score": .30, "data_ready": True}, {}, before, Settings(_env_file=None))["action"] == "BUY"
    after = validate_news(before, dart_report(), now=NOW)
    assert after["dart_validation"]["checks"][0]["status"] == "contradicted"
    assert not after["contribution_allowed"] and after["score"] == 0
    assert after["eligible_sources"] == 1
    assert decide({"score": .30, "data_ready": True}, {}, after, Settings(_env_file=None))["action"] == "HOLD"


@pytest.mark.parametrize("title", [
    "Samsung Electronics Q3 2026 revenue grew 30% YoY",
    "Samsung Electronics consolidated Q2 2026 revenue grew 30% YoY",
    "Samsung Electronics consolidated Q3 2025 revenue grew 30% YoY",
    "Samsung Electronics consolidated Q3 2026 revenue grew 30%",
    "Samsung Electronics consolidated revenue grew 30% YoY",
    "Samsung Electronics standalone consolidated Q3 2026 revenue grew 30% YoY",
])
def test_incomparable_claims_are_never_marked_true_or_false(title):
    checked = validate_news(economics(news(title)), dart_report(), now=NOW)
    assert checked["dart_validation"]["checks"][0]["status"] == "unverifiable"
    assert not checked["dart_validation"]["blocked_urls"]


def test_cumulative_and_quarterly_claims_are_not_mixed():
    rows = accounts()
    rows[0]["thstrm_add_amount"] = "390000"  # YTD 30%; quarter remains 10%.
    checked = validate_news(economics(news("Samsung Electronics consolidated Q3 2026 YTD revenue grew 30% YoY")),
                            dart_report(rows), now=NOW)
    assert checked["dart_validation"]["checks"][0]["status"] == "consistent"


@pytest.mark.parametrize("kind", ["future", "stale", "wrong_symbol", "missing", "old_filing"])
def test_unavailable_or_out_of_cutoff_data_preserves_original_policy(kind):
    report = dart_report()
    if kind == "future":
        report["financials"]["observed_at"] = (NOW+timedelta(seconds=1)).isoformat()
    elif kind == "stale":
        report["financials"]["observed_at"] = (NOW-timedelta(days=2)).isoformat()
    elif kind == "wrong_symbol":
        report["symbol"] = "000660"
    elif kind == "old_filing":
        report["financials"]["filing_date"] = "20250101"
    else:
        report = None
    before = economics(news())
    after = validate_news(before, report, now=NOW)
    assert after["score"] == before["score"]
    assert after["dart_validation"]["status"] == "unavailable"


def test_engine_blocks_contradiction_in_policy_and_llm_and_audits(tmp_path):
    settings = Settings(_env_file=None, watchlist=SYMBOL, news_use_in_llm=True)
    api, llm, news_service, dart_service = Mock(), Mock(), Mock(), Mock()
    api.portfolio.return_value = {"availableCash": 100000000, "totalAssetValue": 100000000, "holdings": []}
    api.limits.return_value = {}
    api.prices.return_value = {"result": [{"symbol": SYMBOL, "price": 70000}]}
    api.candles.return_value = {"candles": []}
    llm.assess.return_value = {"availability": "disabled"}
    news_service.collect.return_value = news("Samsung Electronics consolidated Q3 2026 revenue grew 30% YoY")
    dart_service.collect.return_value = dart_report()
    store = Store(str(tmp_path / "audit.db"))
    with patch("app.engine.technical_signal", return_value={"data_ready": True, "score": .3}):
        result = TradingEngine(settings, api, llm, store, news_service, clock=lambda: NOW,
                               dart=dart_service).run_cycle(scan_only=True)[0]
    assert result["action"] == "HOLD"
    assert len(llm.assess.call_args.kwargs["news"]["articles"]) == 1
    saved = json.loads(store.db.execute("SELECT report FROM policy_decisions").fetchone()[0])
    assert saved["dart"]["financials"]["receipt"] == RECEIPT
    assert saved["economic_news"]["dart_validation"]["checks"][0]["status"] == "contradicted"
    api.place_order.assert_not_called()
    store.db.close()


def test_article_removal_explanation_reuses_recorded_dart_without_network():
    result = explain({"data_ready": True, "score": .3}, {},
        news("Samsung Electronics consolidated Q3 2026 revenue grew 30% YoY"),
        SYMBOL, ["Samsung Electronics"], Settings(_env_file=None), now=NOW, dart_report=dart_report())
    assert result["decision"]["action"] == "HOLD"
    assert result["network_calls"] == 0


def test_annual_data_is_not_used_to_validate_standalone_fourth_quarter():
    report = dart_report()
    report["financials"].update(year=2025, quarter=4, period="annual")
    checked = validate_news(economics(news("Samsung Electronics consolidated Q4 2025 revenue grew 30% YoY")),
                            report, now=NOW)
    assert checked["dart_validation"]["checks"][0]["status"] == "unverifiable"


def test_pagination_failure_never_uses_partial_filings(tmp_path):
    payload = filings()
    payload["total_page"] = 11
    session = Mock()
    session.get.side_effect = [corp_response()] + [response(payload) for _ in range(10)]
    result = client(tmp_path, session).collect(SYMBOL)
    assert result["status"] == "unavailable"
    assert result["reason"] == "DART_FILINGS_TRUNCATED"
    assert result["financials"] is None


def test_no_filings_is_not_a_transport_error(tmp_path):
    session = Mock()
    session.get.side_effect = [corp_response(), response({"status": "013"})]
    result = client(tmp_path, session).collect(SYMBOL)
    assert result["status"] == "ok"
    assert result["financial_status"] == "no_supported_calendar_year_report"


def test_dart_exception_preserves_engine_baseline_and_audit(tmp_path):
    settings = Settings(_env_file=None, watchlist=SYMBOL)
    api, llm, dart_service = Mock(), Mock(), Mock()
    api.portfolio.return_value = {"availableCash": 100000000, "totalAssetValue": 100000000, "holdings": []}
    api.limits.return_value = {}
    api.prices.return_value = {"result": [{"symbol": SYMBOL, "price": 70000}]}
    api.candles.return_value = {"candles": []}
    llm.assess.return_value = {"availability": "disabled"}
    dart_service.collect.side_effect = requests.Timeout("sensitive-url")
    store = Store(str(tmp_path / "audit.db"))
    with patch("app.engine.technical_signal", return_value={"data_ready": True, "score": .4}):
        result = TradingEngine(settings, api, llm, store, clock=lambda: NOW,
                               dart=dart_service).run_cycle(scan_only=True)[0]
    assert result["action"] == "BUY" and result["execution"] == "SCAN_ONLY"
    assert result["dart"]["reason"] == "DART_SERVICE_ERROR"
    assert "sensitive-url" not in json.dumps(result)
    api.place_order.assert_not_called()
    store.db.close()
