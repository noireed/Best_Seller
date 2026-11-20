from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock, patch

import pytest

from app.config import Settings
from app.storage import Store
from app.gemini_client import GeminiClient
from app.gemini_review import GeminiReview
from app.decision_policy import decide
from app.engine import TradingEngine
from app.event_outcomes import filing_outcomes, available_history
from app.intraday_disclosures import IntradayDisclosures
from app.strategy_comparison import prepare, simulate

NOW = datetime.fromisoformat("2026-11-20T02:00:00+00:00")
QUOTE = "Samsung Electronics operating profit declined 30% year on year following a production halt."


def research(domains=("one.example", "two.example")):
    return {"status": "ok", "symbol": "005930", "observed_at": NOW.isoformat(), "text": QUOTE,
            "aliases": ["Samsung Electronics"],
            "sources": [{"id": i, "url": "https://"+domain, "domain": domain, "source_group": domain}
                        for i, domain in enumerate(domains)],
            "claims": [{"text": QUOTE, "source_ids": list(range(len(domains)))}]}


def answer(**updates):
    return {"symbol": "005930", "verdict": "PASS", "claim_id": 0, "evidence_quote": QUOTE,
            "event_date": "2026-11-20", "event_kind": "earnings", "reason": "경제적 근거", "counter_case": "반대 근거",
            "missing_inputs": [], **updates}


@pytest.fixture
def reviewer(tmp_path):
    store = Store(str(tmp_path/"trading.db"))
    obj = GeminiReview(Settings(_env_file=None, gemini_api_key="test", watchlist="005930"), store)
    yield obj
    store.db.close()


def review(obj, a, *, r=None, action="BUY", held=0):
    obj.client.generate = Mock(return_value={"status": "ok", "text": json.dumps(a), "observed_at": NOW.isoformat()})
    return obj.review("005930", {}, None, NOW, technical={"score": .5}, proposed_action=action,
                      holding={"quantity": held}, research=r or research())


@pytest.mark.parametrize("updates", [
    {"symbol": "000660"}, {"claim_id": True}, {"claim_id": 9},
    {"claim_id": -1}, {"evidence_quote": "invented quote"}, {"verdict": "BUY"},
    {"reason": ""}, {"counter_case": ""},
])
def test_invalid_or_invented_opinion_never_approves(reviewer, updates):
    assert review(reviewer, answer(**updates))["status"] == "invalid_response"


def test_source_must_support_the_actual_quote(reviewer):
    r = research();r["claims"].append({"text": "A completely different claim about the company.", "source_ids": [1]})
    result = review(reviewer, answer(claim_id=1), r=r)
    assert result["status"] == "unsupported_citation" and result["verdict"] == "HOLD"


def test_freshness_is_observation_not_cache_hit(reviewer):
    r = research();r["observed_at"] = (NOW-timedelta(seconds=901)).isoformat()
    result = review(reviewer, answer(), r=r)
    assert result["status"] == "stale_research"
    reviewer.client.generate.assert_not_called()


@pytest.mark.parametrize("ids,day,kind,held", [([0],"2026-11-20","earnings",10),
    ([0,1],"2026-11-17","earnings",10), ([0,1],"2026-11-21","earnings",10),
    ([0,1],"2026-11-20","other",10), ([0,1],"2026-11-20","earnings",0)])
def test_event_exit_rejects_uncorroborated_old_future_noise(reviewer, ids, day, kind, held):
    r = research();r["claims"][0]["source_ids"] = ids
    result = review(reviewer, answer(verdict="EXIT",event_date=day,event_kind=kind),r=r,action="HOLD",held=held)
    assert result.get("verdict") != "EXIT"


def test_exit_requires_rule_confirmed_economic_evidence(reviewer):
    r=research();r["text"]=r["claims"][0]["text"]="Samsung Electronics share price fell in a volatile market."
    a=answer(verdict="EXIT",evidence_quote=r["text"])
    assert review(reviewer,a,r=r,action="HOLD",held=10)["status"] == "exit_not_corroborated"


def test_economic_exit_and_price_exit(reviewer):
    assert review(reviewer,answer(verdict="EXIT"),action="HOLD",held=10)["verdict"] == "EXIT"
    assert review(reviewer,answer(verdict="HOLD"),action="SELL",held=10)["verdict"] == "PASS"


def test_positive_news_cannot_cancel_price_exit():
    s=Settings(_env_file=None)
    news={"contribution_allowed":True,"score":1,"eligible_sources":2}
    current=decide({"data_ready":True,"score":-.4},{},news,s)
    old=decide({"data_ready":True,"score":-.4},{},news,s.model_copy(update={"protect_sell_signal":False}))
    assert current["action"] == "SELL" and old["action"] == "HOLD"
    assert current["news_adjustment"] == 0 and current["news_raw_adjustment"] > 0


def test_cache_reservation_survives_restart_and_does_not_refresh_time(tmp_path):
    path=str(tmp_path/"db.sqlite"); s=Settings(_env_file=None,gemini_api_key="secret",watchlist="005930",gemini_daily_budget=2)
    response=Mock(status_code=200);response.json.return_value={"candidates":[{"content":{"parts":[{"text":"OK"}]}}]}
    session=Mock();session.post.return_value=response
    store=Store(path); checkpoints=[];store.order_checkpoint=lambda:checkpoints.append(True)
    c=GeminiClient(s,store,session=session)
    assert c.generate("a",{},NOW)["status"] == "ok"
    cached=c.generate("a",{},NOW+timedelta(seconds=30))
    assert cached["cached"] and cached["observed_at"] == NOW.isoformat()
    assert len(checkpoints) == 1
    store.db.close()
    store=Store(path);c=GeminiClient(s,store,session=session)
    assert c.generate("b",{},NOW)["status"] == "daily_budget"
    assert c.generate("a",{},NOW)["cached"]
    assert session.post.call_count == 1
    assert "secret" not in str(store.db.execute("SELECT * FROM gemini_cache").fetchall())
    store.db.close()


def test_http_error_is_visible_not_an_approval(tmp_path):
    store=Store(str(tmp_path/"db"));session=Mock();session.post.return_value.status_code=429
    session.post.return_value.json.return_value={"error":{"status":"RESOURCE_EXHAUSTED","details":[{"reason":"QUOTA_EXCEEDED"}]}}
    c=GeminiClient(Settings(_env_file=None,gemini_api_key="test"),store,session=session)
    r=c.generate("a",{},NOW)
    assert r["status"] == "http_error" and r["error_reasons"] == ["QUOTA_EXCEEDED"]
    assert c.generate("a",{},NOW)["cached"]
    assert session.post.call_count == 1
    store.db.close()


def engine_case(tmp_path, score, opinion, *, required=True):
    s=Settings(_env_file=None,watchlist="005930",gemini_api_key="test",gemini_required_for_buy=required)
    api=Mock();llm=Mock();store=Store(str(tmp_path/"db"))
    api.portfolio.return_value={"totalAssetValue":100000000,"availableCash":90000000,"holdings":[{"symbol":"005930","quantity":10,"availableQuantity":10}]}
    api.limits.return_value={"remainingTrades":30,"cooldowns":[]}
    api.prices.return_value={"result":[{"symbol":"005930","price":70000,"timestamp":NOW.isoformat()}]}
    api.candles.return_value={"candles":[]};llm.assess.return_value={"availability":"disabled"}
    e=TradingEngine(s,api,llm,store,clock=lambda:NOW)
    e.gemini.search=Mock(return_value=research());e.gemini.review=Mock(return_value=opinion)
    with patch("app.engine.technical_signal",return_value={"data_ready":True,"score":score}):
        row=e.run_cycle(scan_only=True)[0]
    api.place_order.assert_not_called();store.db.close()
    return row


@pytest.mark.parametrize("opinion", [{"status":"http_error","http_status":429},{"status":"invalid_response"},
    {"status":"daily_budget"},{"status":"ok","verdict":"HOLD","reason":"uncertain"}])
def test_engine_never_buys_without_positive_confirmation(tmp_path,opinion):
    row=engine_case(tmp_path,.5,opinion)
    assert row["action"] == "HOLD" and row["quantity"] == 0


def test_engine_confirmed_buy_and_preserved_sell(tmp_path):
    assert engine_case(tmp_path,.5,{"status":"ok","verdict":"PASS"})["action"] == "BUY"
    assert engine_case(tmp_path,-.4,{"status":"http_error","http_status":429})["action"] == "SELL"
    assert engine_case(tmp_path,0,{"status":"ok","verdict":"EXIT","reason":"material"})["action"] == "SELL"


def test_market_wide_dart_pages_once_and_preserves_financials():
    client=Mock();client._now.return_value=NOW
    def request(endpoint,params,**kwargs):
        if endpoint == "corpCode.xml":return {"005930":"00126380","000660":"00164779"},NOW.isoformat()
        assert "pblntf_ty" not in params
        assert params["bgn_de"] == params["end_de"] == "20261120"
        return {"total_page":2,"list":[{"corp_code":"00126380","rcept_no":"20261120000001","rcept_dt":"20261120","report_nm":"Major contract"}] if params["page_no"] == 1 else []},NOW.isoformat()
    client._request.side_effect=request
    baseline=Mock();baseline.collect.side_effect=lambda s:{"symbol":s,"status":"ok","financials":{"metrics":{"revenue":1}}}
    svc=IntradayDisclosures(baseline,client)
    assert len(svc.collect("005930")["intraday"]["filings"]) == 1
    second=svc.collect("000660")
    assert second["financials"]["metrics"]["revenue"] == 1 and second["intraday"]["filings"] == []
    assert client._request.call_count == 3


def test_event_outcomes_enter_after_filing_and_exclude_unfinished_horizons():
    history={"005930":[{"timestamp":f"2026-11-{day:02}T00:00:00Z","openPrice":100,"closePrice":110} for day in range(1,21)]}
    filings={"005930":[{"receipt":"r1","date":"20261118","title":"quarterly report","url":"https://dart.fss.or.kr/x"}]}
    lib=filing_outcomes(history,filings,as_of=NOW)
    case=lib["005930"]["cases"][0]
    assert case["entry"] == "2026-11-19" and set(case["horizons"]) == {"1"}
    assert 0 < case["horizons"]["1"]["net_pct"] < case["horizons"]["1"]["gross_pct"]
    assert not available_history(lib,"005930",NOW-timedelta(days=1))


@pytest.mark.parametrize("price", [float("nan"),float("inf"),True,-1])
def test_comparison_rejects_invalid_prices(price):
    with pytest.raises(ValueError):
        prepare({"005930":[{"timestamp":"2026-01-01T00:00:00Z","openPrice":price,"closePrice":100}]},as_of=NOW)


@pytest.mark.parametrize("fresh_price,fresh_time,expected", [
    (100000,NOW,"DRY_RUN"),
    (100000,NOW+timedelta(minutes=1),"BLOCKED: 시세 시각 누락·지연·미래"),
    (0,NOW,"BLOCKED: 주문 직전 시세 재확인 실패"),
])
def test_order_quote_is_refreshed_and_size_recalculated(tmp_path,fresh_price,fresh_time,expected):
    s=Settings(_env_file=None,watchlist="005930",gemini_required_for_buy=False)
    api=Mock();llm=Mock();store=Store(str(tmp_path/"db"))
    api.portfolio.return_value={"totalAssetValue":100000000,"availableCash":100000000,"holdings":[]}
    api.limits.return_value={"remainingTrades":30,"cooldowns":[]}
    api.prices.side_effect=[{"result":[{"symbol":"005930","price":70000,"timestamp":NOW.isoformat()}]},
        {"result":[{"symbol":"005930","price":fresh_price,"timestamp":fresh_time.isoformat()}]}]
    api.candles.return_value={"candles":[]};llm.assess.return_value={"availability":"disabled"}
    with patch("app.engine.technical_signal",return_value={"data_ready":True,"score":.5}):
        row=TradingEngine(s,api,llm,store,clock=lambda:NOW).run_cycle()[0]
    assert row["execution"] == expected
    if fresh_price>0:assert row["price"] == 100000 and row["quantity"] == 20
    api.place_order.assert_not_called();store.db.close()


def test_reservation_failure_cannot_make_a_billable_call(tmp_path):
    store=Store(str(tmp_path/"db"));session=Mock()
    store.order_checkpoint=Mock(side_effect=RuntimeError("checkpoint"))
    client=GeminiClient(Settings(_env_file=None,gemini_api_key="test"),store,session=session)
    with pytest.raises(RuntimeError):client.generate("a",{},NOW)
    session.post.assert_not_called()
    assert client.reserved == 0
    store.db.close()
